#!/bin/bash
# Install required package
    apt-get update -y
    apt-get install -y build-essential linux-headers-$(uname -r) xinit i3 terminator vim usbutils screen iw wpasupplicant

# Configure wifi
## Deploy simulated wifi card
echo "mac80211_hwsim" > /etc/modules-load.d/hwsim.conf
echo "options mac80211_hwsim radios=2" > /etc/modprobe.d/hwsim.conf
        

## Create configuration file for wpa_supplicant
    cat > /root/wpa.conf << EOF
network={
    ssid="test"
    psk="testtesttest"
}
EOF

# reboot
reboot
