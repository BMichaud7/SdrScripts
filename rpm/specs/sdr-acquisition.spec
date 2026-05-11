Name:           sdr-acquisition
Version:        1.0.0
Release:        1%{?dist}
Summary:        SDR Acquisition App — IQ collection and task management
License:        Proprietary
BuildArch:      x86_64

Requires:       qpid-proton-cpp
Requires:       tinyxml2
Requires:       spdlog
Requires:       fmt
Requires:       fftw-libs-single
Requires:       libpq
Requires(post): systemd
Requires(preun): systemd
Requires(postun): systemd

%description
SDR Acquisition App daemon.
Listens on the AMQP bus, receives spectrum scan results, collects
IQ data from the controller, and stores detections in PostgreSQL.

%prep
%build

%install
# Binary
install -D -m 0755 %{sdr_stagedir_acq}/bin/sdr_acquisition \
    %{buildroot}%{_bindir}/sdr_acquisition

# Bundle libpqxx (not in EPEL for el10) into a private lib dir
install -d %{buildroot}%{_libdir}/sdr-acquisition
cp -P %{pqxx_libdir}/libpqxx*.so* \
    %{buildroot}%{_libdir}/sdr-acquisition/

# Default config
install -D -m 0644 %{_sourcedir}/configs/scanner.xml \
    %{buildroot}%{_sysconfdir}/sdr-acquisition/scanner.xml

# Sysconfig env file
install -D -m 0640 %{_sourcedir}/configs/sdr-acquisition.sysconfig \
    %{buildroot}%{_sysconfdir}/sysconfig/sdr-acquisition

# Systemd unit
install -D -m 0644 %{_sourcedir}/systemd/sdr-acquisition.service \
    %{buildroot}%{_unitdir}/sdr-acquisition.service

# ld.so config so system finds our bundled libpqxx
install -D -m 0644 /dev/stdin \
    %{buildroot}%{_sysconfdir}/ld.so.conf.d/sdr-acquisition.conf << 'EOF'
%{_libdir}/sdr-acquisition
EOF

%files
%{_bindir}/sdr_acquisition
%dir %{_libdir}/sdr-acquisition
%{_libdir}/sdr-acquisition/libpqxx*.so*
%dir %{_sysconfdir}/sdr-acquisition
%config(noreplace) %{_sysconfdir}/sdr-acquisition/scanner.xml
%config(noreplace) %{_sysconfdir}/sysconfig/sdr-acquisition
%{_unitdir}/sdr-acquisition.service
%config %{_sysconfdir}/ld.so.conf.d/sdr-acquisition.conf

%pre
getent group sdr  >/dev/null || groupadd -r sdr
getent passwd sdr >/dev/null || \
    useradd -r -g sdr -d /var/lib/sdr -s /sbin/nologin \
            -c "SDR service account" sdr
exit 0

%post
ldconfig
%systemd_post sdr-acquisition.service

%preun
%systemd_preun sdr-acquisition.service

%postun
ldconfig
%systemd_postun_with_restart sdr-acquisition.service

%changelog
* Sun May 11 2026 SDR Project <sdr@local> - 1.0.0-1
- Initial RPM packaging
