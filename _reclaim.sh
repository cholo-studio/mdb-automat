#!/bin/bash
cd /opt/mdb-dashboard || exit 1
S=/opt/mdb-dashboard/_reclaim_status.txt
echo "START $(date +%T)" > "$S"
rm -f logs_archive/mdb_sniffer_20260716.log.zst
zstd -T0 -9 -q -f -o logs_archive/mdb_sniffer_20260716.log.zst mdb_sniffer.log
echo "sniffer_kompr_fertig $(date +%T)" >> "$S"
if zstd -t logs_archive/mdb_raw_20260716.log.zst >>"$S" 2>&1 && zstd -t logs_archive/mdb_sniffer_20260716.log.zst >>"$S" 2>&1; then
  echo "ARCHIVE_OK" >> "$S"
  sudo systemctl stop mdb-dashboard.service
  echo "service_gestoppt $(date +%T)" >> "$S"
  : > mdb_raw.log
  : > mdb_sniffer.log
  echo "logs_geleert" >> "$S"
  sudo systemctl start mdb-dashboard.service
  echo "service_gestartet $(date +%T)" >> "$S"
  sleep 4
  echo -n "service_status=" >> "$S"; systemctl is-active mdb-dashboard.service >> "$S"
  df -h / | tail -1 >> "$S"
  echo "DONE" >> "$S"
else
  echo "ARCHIV_VERIFY_FEHLGESCHLAGEN_KEIN_TRUNCATE" >> "$S"
fi
