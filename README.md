# virtualBox_wifiCardEmulator
VirtualBox is nice to quickly experiment many things on an isolated environment.
However, it has some limitations. Especially, it does not provide wifi card to virtual machines, and it is not possible to deploy an access point.

The aim of this project is to details the way we can emulate a wifi card. The data transmited on or received by the wifi card is relayed on ethernet.

For this project, two machines are configured : one is the access point, and the other is the client. Both are on linux debian.
Make sure that, for each machine, an internal network is configured. The following picture shows such configuration for one machine. For the other, make sure that the mac address is different.

<img width="399" height="305" alt="image" src="https://github.com/user-attachments/assets/3a8f8b84-5222-422d-83a9-cfaf986be584" />

**We assume that the ethernet interface for each guest machine is enp0s9**

# Configuring AP
- As root, run AP.sh. The script will reboot the machine
- set IP (This has to be done on each reboot)

```bash
        ip addr add 10.20.30.254/24 dev wlan0
        ip addr add 192.168.2.254/24 dev enp0s9
        ip link set enp0s9 up
```

- execute the relay : `python3 /mnt/shared/hwsim_relay.py --peer 192.168.2.253`


# Configure client
- As root, run client_wifi.sh. The script will reboot the machine
- set IP (This has to be done on each reboot)

```bash
        ip addr add 192.168.2.253/24 dev enp0s9
        ip link set enp0s9 up
```

- execute the relay : `python3 /mnt/shared/hwsim_relay.py --peer 192.168.2.254 --local-mac 42:00:00:00:01:00`

