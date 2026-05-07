#!/bin/bash

# Install required package
apt-get update -y
apt-get install -y build-essential linux-headers-$(uname -r) xinit i3 terminator vim usbutils screen hostapd dnsmasq iw


# Configure wifi
## Deploy simulated wifi card
echo "mac80211_hwsim" > /etc/modules-load.d/hwsim.conf
echo "options mac80211_hwsim radios=1" > /etc/modprobe.d/hwsim.conf

## Deploy access point
mkdir -p /etc/hostapd
echo -n "wifi password: "
echo "interface=wlan0
hw_mode=g
channel=10
ieee80211d=1
country_code=FR
ieee80211n=1
wmm_enabled=1
ssid=test
auth_algs=1
wpa=2
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
wpa_passphrase=testtesttest
ignore_broadcast_ssid=0" > /etc/hostapd/hostapd.conf

chown root:root /etc/hostapd/hostapd.conf
chmod 700 /etc/hostapd/hostapd.conf


systemctl unmask hostapd
systemctl enable hostapd

## Deploy DHCP server
echo "interface=wlan0" >> /etc/dnsmasq.conf
echo "dhcp-range=10.20.30.100,10.20.30.200,12h" >> /etc/dnsmasq.conf
echo >> /etc/dnsmasq.conf


# reboot
reboot

