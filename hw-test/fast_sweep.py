#!/usr/bin/env python3
"""
Fast sweep: 20 MHz BW + 0.3s dwell = ~45s for 80–1000 MHz.
Two speed modes:
  --mode fast   : 20 MHz BW, 0.3s dwell  (~45s)
  --mode turbo  : 20 MHz BW, 0.15s dwell (~30s, slightly noisier)
  --mode ludicrous : 20 MHz BW, 0.05s dwell (~20s, rough detection only)

PYTHONPATH=/tmp/proton_pkg python3 fast_sweep.py [--mode fast|turbo|ludicrous]
"""
from __future__ import annotations
import argparse, json, socket, struct, threading, time, uuid
import numpy as np
import proton, proton.handlers, proton.reactor

BROKER   = "amqp://localhost:5672"
REQ_Q    = "sdr.task.request"
RESP_Q   = "sdr.task.response"
CREDS    = ("sdr_ctrl", "sdr_hw_test")
DEST_IP  = "127.0.0.1"
IQ_HDR   = struct.Struct("<I I Q Q I H B B")
IQ_MAGIC = 0x49515030

MODES = {
#   mode          BW_hz   SR_sps   step_hz  dwell_s  fft
    "fast":      (20e6,   20e6,    16e6,    0.30,    8192),
    "turbo":     (20e6,   20e6,    16e6,    0.15,    8192),
    "ludicrous": (20e6,   20e6,    16e6,    0.05,    4096),
}

START_HZ = 80e6
STOP_HZ  = 1000e6
GAIN_DB  = 40.0
THRESH   = 10.0   # dB above noise floor


class _H(proton.handlers.MessagingHandler):
    def __init__(self, s): super().__init__(); self._s = s
    def on_start(self, ev):
        c = ev.container.connect(BROKER, user=CREDS[0], password=CREDS[1],
                                 sasl_enabled=True, allowed_mechs="PLAIN")
        ev.container.create_receiver(c, RESP_Q)
        self._sender = ev.container.create_sender(c, REQ_Q)
        self._s._h = self
    def on_sendable(self, ev): self._s._ready.set()
    def on_message(self, ev):
        try: msg = json.loads(ev.message.body)
        except: return
        rid = msg.get("request_id","")
        with self._s._lk:
            e = self._s._pend.get(rid)
        if e: e[1].append(msg); e[0].set()
    def send(self, d):
        self._sender.send(proton.Message(body=json.dumps(d),
                                          content_type="application/json"))

class Sess:
    def __init__(self):
        self._pend={}; self._lk=threading.Lock()
        self._ready=threading.Event(); self._h=None
        self._c=proton.reactor.Container(_H(self))
        threading.Thread(target=self._c.run,daemon=True).start()
        self._ready.wait(10)
    def rpc(self,req,t=20):
        rid=req["request_id"]; ev=threading.Event(); box=[]
        with self._lk: self._pend[rid]=(ev,box)
        self._h.send(req); ev.wait(t)
        with self._lk: self._pend.pop(rid,None)
        return box[0] if box else None
    def fire(self,req): self._h.send(req)
    def close(self):
        try: self._c.stop()
        except: pass


def rx(port, secs, sr):
    s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET,socket.SO_RCVBUF,16*1024*1024)
    s.settimeout(min(0.08, secs * 0.25)); s.bind(("",port))
    chunks=[]; t=time.time()+secs
    try:
        while time.time()<t:
            try: data=s.recv(65536)
            except socket.timeout: break
            if len(data)<IQ_HDR.size: continue
            f=IQ_HDR.unpack_from(data)
            if f[0]!=IQ_MAGIC: continue
            n=f[5]; raw=np.frombuffer(data[IQ_HDR.size:IQ_HDR.size+n*8],dtype=np.float32)
            if len(raw)==n*2: chunks.append(raw[0::2]+1j*raw[1::2])
    finally: s.close()
    return np.concatenate(chunks) if chunks else np.array([],dtype=np.complex64)


def peaks(iq, cf, sr, n_fft, thresh):
    if len(iq) < n_fft*2: return []
    win=np.blackman(n_fft); step=n_fft//2
    nfr=max(1,(len(iq)-n_fft)//step); acc=np.zeros(n_fft)
    for i in range(nfr):
        seg=iq[i*step:i*step+n_fft]*win
        acc+=np.abs(np.fft.fft(seg))**2
    acc/=nfr
    db=np.fft.fftshift(10*np.log10(acc+1e-30))
    freqs=np.fft.fftshift(np.fft.fftfreq(n_fft,1/sr))+cf
    noise=np.percentile(db,30); thr=noise+thresh
    # inner 80% only
    m=int(n_fft*0.10); db2=db[m:-m]; fr2=freqs[m:-m]
    guard=max(1,int(30e3/(sr/n_fft)))
    rem=db2.copy(); out=[]
    for _ in range(40):
        idx=int(np.argmax(rem))
        if rem[idx]<thr: break
        # −3dB BW
        lo=idx; hi=idx
        while lo>0 and rem[lo]>rem[idx]-3: lo-=1
        while hi<len(rem)-1 and rem[hi]>rem[idx]-3: hi+=1
        bw=float(fr2[hi]-fr2[lo])
        out.append({"freq_hz":float(fr2[idx]),"freq_mhz":float(fr2[idx]/1e6),
                    "bw_hz":max(bw,sr/n_fft),"bw_khz":max(bw/1e3,sr/n_fft/1e3),
                    "above_db":float(rem[idx]-noise)})
        rem[max(0,idx-guard):min(len(rem),idx+guard+1)]=noise-99
    return out


def classify(f,bw):
    if 87.5e6<=f<=108e6: return "FM broadcast" if bw>50e3 else "FM pilot/RDS"
    if 108e6<=f<118e6: return "Aviation nav (VOR/ILS)"
    if 118e6<=f<136e6: return "Aircraft voice (AM)"
    if 144e6<=f<148e6: return "2m amateur"
    if 148e6<=f<174e6: return "VHF public safety / marine"
    if 162.3e6<=f<=162.6e6: return "NOAA weather"
    if 174e6<=f<230e6: return "DAB/DVB-T" if bw>1e6 else "VHF-hi NFM"
    if 230e6<=f<400e6: return "TETRA/DMR" if 100e3<bw<500e3 else "UHF NFM/military"
    if 433e6<=f<=434.8e6: return "ISM 433 MHz"
    if 430e6<=f<470e6: return "70cm / UHF land mobile"
    if 470e6<=f<790e6:
        if bw>3e6: return "DVB-T (digital TV)"
        if bw>200e3: return "LTE/4G (cellular DL)"
        return "UHF digital/NFM"
    if 791e6<=f<870e6: return "LTE 800 MHz"
    if 868e6<=f<870e6: return "LoRa/SigFox/LTE IoT"
    if 870e6<=f<960e6:
        if bw>500e3: return "GSM/LTE 900"
        return "ISM 915 / cellular"
    return "wideband" if bw>500e3 else "narrowband"


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--mode",default="fast",choices=list(MODES))
    args=ap.parse_args()

    bw,sr,step,dwell,n_fft=MODES[args.mode]
    centers=[]
    cf=START_HZ+bw/2
    while cf-bw/2<STOP_HZ: centers.append(cf); cf+=step
    n=len(centers)
    bin_hz=sr/n_fft

    print(f"{'='*68}")
    print(f"  Fast sweep {START_HZ/1e6:.0f}–{STOP_HZ/1e6:.0f} MHz  [{args.mode} mode]")
    print(f"  {n} steps × {bw/1e6:.0f} MHz BW  |  {dwell*1000:.0f}ms dwell  |  "
          f"gain={GAIN_DB:.0f}dB  |  {bin_hz/1e3:.1f}kHz/bin")
    print(f"{'='*68}")

    sess=Sess(); found=[]; t0=time.time()
    for i,cf in enumerate(centers):
        eta=(time.time()-t0)/max(i,1)*(n-i) if i else 0
        print(f"[{i+1:3d}/{n}] {(cf-bw/2)/1e6:5.0f}–{(cf+bw/2)/1e6:5.0f}MHz "
              f"ETA:{eta:.0f}s",end="  ",flush=True)

        rid=str(uuid.uuid4())
        resp=sess.rpc({
            "msg_type":"TASK_REQUEST","schema_version":"2.0",
            "request_id":rid,"timestamp_ms":int(time.time()*1000),
            "task_type":"WIDEBAND","rank":2,
            "schedule":{"mode":"IMMEDIATE","duration_ms":int(dwell*1000)+800},
            "rf":{"center_freq_hz":cf,"bandwidth_hz":bw,"sample_rate_sps":sr,
                  "rx_count":1,"rx_gain_db":[GAIN_DB],"rx_agc":[False]},
            "streaming":{"dest_ip":DEST_IP},
            "wideband":{"record_raw_iq":True,"fft_size":2048},
        },t=20)

        if not resp or resp.get("status")!="ACCEPTED":
            print(f"SKIP({resp.get('reject_reason','?') if resp else 'timeout'})")
            continue

        port=resp["streams"][0]["udp_port"]; tid=resp["task_id"]
        iq=rx(port,dwell,sr)
        sess.fire({"msg_type":"TASK_STOP","request_id":str(uuid.uuid4()),
                   "task_id":tid,"timestamp_ms":int(time.time()*1000),"reason":"done"})

        sigs=peaks(iq,cf,sr,n_fft,THRESH)
        # filter spurs: min 5 kHz BW or very strong
        sigs=[s for s in sigs if s["bw_hz"]>=5e3 or s["above_db"]>=22]
        # de-dup vs already found
        new=[s for s in sigs if not any(abs(s["freq_hz"]-p["freq_hz"])<300e3 for p in found)]
        if new:
            print(f"→ {len(new)}")
            for s in new:
                s["type"]=classify(s["freq_hz"],s["bw_hz"])
                print(f"       {s['freq_mhz']:8.3f}MHz  BW={s['bw_khz']:6.1f}kHz "
                      f"+{s['above_db']:5.1f}dB  {s['type']}")
            found.extend(new)
        else:
            print("clear")

    sess.close()
    elapsed=time.time()-t0
    print(f"\n{'='*68}")
    print(f"  DONE — {elapsed:.0f}s — {len(found)} signal(s)")
    print(f"{'='*68}")
    print(f"  {'Freq':>10}  {'BW(kHz)':>9}  {'+dB':>6}  Type")
    print(f"  {'-'*60}")
    for s in sorted(found,key=lambda x:x["freq_hz"]):
        print(f"  {s['freq_mhz']:10.3f}  {s['bw_khz']:9.1f}  {s['above_db']:6.1f}  {s['type']}")
    print()

if __name__=="__main__": main()
