Name:           sdr-analysis
Version:        2.3.0
Release:        1%{?dist}
Summary:        SDR Analysis App — RF signal auto-identification service
License:        Proprietary
BuildArch:      x86_64

Requires:       qpid-proton-cpp
Requires:       tinyxml2
Requires:       fftw-libs-single
Requires:       jsoncpp
Requires:       openssl-libs
Requires:       cyrus-sasl-plain
Requires:       libuuid
Requires(post): systemd
Requires(preun): systemd
Requires(postun): systemd

%description
SDR Analysis App daemon.
Receives IQ streams, runs signal identification algorithms (P25,
DMR, AM/FM demodulation detection), and publishes results on the
AMQP bus.

%prep
%build

%install
install -D -m 0755 %{sdr_stagedir_analysis}/bin/sdr_analysis \
    %{buildroot}%{_bindir}/sdr_analysis

install -D -m 0644 %{_sourcedir}/configs/analysis.xml \
    %{buildroot}%{_sysconfdir}/sdr-analysis/analysis.xml

install -D -m 0640 %{_sourcedir}/configs/sdr-analysis.sysconfig \
    %{buildroot}%{_sysconfdir}/sysconfig/sdr-analysis

install -D -m 0644 %{_sourcedir}/systemd/sdr-analysis.service \
    %{buildroot}%{_unitdir}/sdr-analysis.service

%files
%{_bindir}/sdr_analysis
%dir %{_sysconfdir}/sdr-analysis
%config(noreplace) %{_sysconfdir}/sdr-analysis/analysis.xml
%config(noreplace) %{_sysconfdir}/sysconfig/sdr-analysis
%{_unitdir}/sdr-analysis.service

%pre
getent group sdr  >/dev/null || groupadd -r sdr
getent passwd sdr >/dev/null || \
    useradd -r -g sdr -d /var/lib/sdr -s /sbin/nologin \
            -c "SDR service account" sdr
exit 0

%post
%systemd_post sdr-analysis.service

%preun
%systemd_preun sdr-analysis.service

%postun
%systemd_postun_with_restart sdr-analysis.service

%changelog
* Sun May 11 2026 SDR Project <sdr@local> - 2.3.0-1
- Initial RPM packaging
