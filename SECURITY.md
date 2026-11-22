# Security

## Report a vulnerability

Report it privately through GitHub's private vulnerability reporting:

1. Open the Security tab of this repository on GitHub.
2. Choose Report a vulnerability.
3. Say what an attacker can do, which version you used, and the steps to see it.

Please do not open a public issue for a security problem. Do not paste sign-in tokens, keychain contents or emails into the report.

## Supported versions

| Version | Supported |
| ------- | --------- |
| 1.0.x   | Yes       |

## What to look at

Lifeline keeps sign-ins for several Claude accounts, writes Claude Code's keychain item and ~/.claude.json, and can talk to one other computer over the network. Reports on any of these are welcome:

1. The keychain and the sign-in files on disk.
2. The pairing, and the signed messages between the Mac and the other computer.
3. The local socket the menu bar app uses to talk to the background service.
4. The browser hand-off during a sign-in.
