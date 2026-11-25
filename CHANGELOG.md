# Changelog

## 1.0.1 (2026-11-25)

### Fixed

1. A switch no longer makes macOS ask for your keychain password again and again. When Claude Code's sign-in passed 4 KB, for example with several MCP server sign-ins, Lifeline 1.0.0 wrote it through the Security framework from Python. macOS then let only Python read the item, so every read by Claude Code asked for the password. Lifeline now writes it with `/usr/bin/security`, as Claude Code does. If you saw the prompts on 1.0.0, type the password once and they stop.

## 1.0.0 (2026-11-22)

The first public release.

### Added

1. A Mac menu bar app that shows each Claude account's 5-hour and weekly use, read live from Anthropic and from Claude Code's status line.
2. Forecasts of when an account runs out at its current pace, with a 5-hour window chart and a 7-day chart for each account.
3. Switching Claude Code on the Mac to another account, by hand or on its own at 99% use. Open Claude Code sessions move to the new account.
4. Mac notifications before an account runs out, at its limit and when it is back. Others come at 90% of a week and 3 days before a sign-in ends.
5. Desktop widgets in small, medium and large sizes.
6. An optional second computer, Linux or Mac, paired with a one-time code. Every request and reply between the two is signed.
7. Adding accounts through Claude's own sign-in page in your browser, on the Mac or on the other computer.
8. Remove from Lifeline for one account, and Reset Lifeline to go back to the first run. Reset saves a copy of the history and settings first. Neither touches Claude Code's own sign-in.
9. A Claude Code status line that shows the account in use and its figures.
10. The lifeline command, with the same actions in the terminal.
