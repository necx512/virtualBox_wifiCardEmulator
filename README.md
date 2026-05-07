# virtualBox_wifiCardEmulator
VirtualBox is nice to quickly experiment many things on an isolated environment.
However, it has some limitations. Especially, it does not provide wifi card to virtual machines, and it is not possible to deploy an access point.

The aim of this project is to details the way we can emulate a wifi card. The data transmited on or received by the wifi card is relayed on ethernet.

For this project, two machines are configured : one is the access point, and the other is the client. Both are on linux.


<img width="399" height="305" alt="image" src="https://github.com/user-attachments/assets/3a8f8b84-5222-422d-83a9-cfaf986be584" />
