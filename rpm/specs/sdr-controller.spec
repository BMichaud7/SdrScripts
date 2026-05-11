Name:           sdr-controller
Version:        2.0.0
Release:        1%{?dist}
Summary:        SDR Radio Resource Manager — task scheduling controller
License:        Proprietary
BuildArch:      x86_64

Requires:       SoapySDR
Requires:       qpid-proton-cpp
Requires:       tinyxml2
Requires:       spdlog
Requires:       fmt
Requires:       fftw-libs-single
Requires:       libuuid
Requires(post): systemd
Requires(preun): systemd
Requires(postun): systemd

%description
SDR Radio Resource Manager controller daemon.
Accepts task requests via AMQP (Artemis), schedules them against
SoapySDR hardware, and streams IQ data to configured UDP endpoints.

# ── Prep / Build (already done in container) ─────────────────────────────────
%prep
%build

# ── Install ───────────────────────────────────────────────────────────────────
%install
# Binary
install -D -m 0755 %{sdr_stagedir_ctrl}/bin/sdr_controller \
    %{buildroot}%{_bindir}/sdr_controller

# Default config (marked noreplace so upgrades don't overwrite site config)
install -D -m 0644 %{_sourcedir}/configs/devices-native.xml \
    %{buildroot}%{_sysconfdir}/sdr-controller/devices.xml

# Sysconfig env file
install -D -m 0640 %{_sourcedir}/configs/sdr-controller.sysconfig \
    %{buildroot}%{_sysconfdir}/sysconfig/sdr-controller

# Systemd unit
install -D -m 0644 %{_sourcedir}/systemd/sdr-controller.service \
    %{buildroot}%{_unitdir}/sdr-controller.service

# ── Files ─────────────────────────────────────────────────────────────────────
%files
%{_bindir}/sdr_controller
%dir %{_sysconfdir}/sdr-controller
%config(noreplace) %{_sysconfdir}/sdr-controller/devices.xml
%config(noreplace) %{_sysconfdir}/sysconfig/sdr-controller
%{_unitdir}/sdr-controller.service

# ── Scriptlets ────────────────────────────────────────────────────────────────
%pre
getent group sdr  >/dev/null || groupadd -r sdr
getent passwd sdr >/dev/null || \
    useradd -r -g sdr -d /var/lib/sdr -s /sbin/nologin \
            -c "SDR service account" sdr
exit 0

%post
%systemd_post sdr-controller.service

%preun
%systemd_preun sdr-controller.service

%postun
%systemd_postun_with_restart sdr-controller.service

%changelog
* Sun May 11 2026 SDR Project <sdr@local> - 2.0.0-1
- Initial RPM packaging
