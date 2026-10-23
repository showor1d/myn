# MYN v1.0

MYN is a Windows desktop assistant with conversation, memory, voice interaction,
alarms, a local media library and an audio console.

## Download and install

Download **MYN-Setup-1.0-x64.exe** from the [v1.0 release](https://github.com/showor1d/myn/releases/tag/v1.0).
Windows 10 version 1809 or later, or Windows 11, is required. Stay online during
setup and allow approximately 14 GB of free space. Setup includes private Python
runtimes and downloads verified speech-recognition components. System Python is
not required. Speech output uses ElevenLabs and requires your own service key.

Keep your `.mynbackup` file and its password separately before reinstalling Windows.
On first launch, choose **Import full backup** to restore it. Personal data and
credentials are not included in the public installer.

## Version baseline

The GitHub main revision `89b4c871cfed6868d40ab3c00fea3166a80e4a26` is the functional
baseline for **v1.0**. This release unifies the source, installer and executable
version metadata and rebuilds the Windows installer from that baseline. Future
releases use v1.0 as their starting point. Local changes made after that revision
are separate until published in a later release.

See [AGENTS.md](AGENTS.md) for current architecture and development rules.
