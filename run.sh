#!/bin/bash

cd /home/pi/serial_utlis

while true
do
    echo "[`date`] Otomatik git pull..."
    git pull --rebase

    echo "[`date`] serial2.py başlatılıyor..."
    python3 serial2.py

    echo "[`date`] Script bitti. 10 saniye sonra tekrar denenecek."
    sleep 1000
done

