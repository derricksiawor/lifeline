# Contributing

Thank you for helping. Bug reports, fixes and small focused features are all welcome. For a large change, open an issue first so we can agree on the shape before you build it.

## Build and run the tests

You need macOS 14 or newer with Xcode or the Command Line Tools (Swift 6), and Python 3.13 or newer as python3. Nothing else is installed: the core uses only Python's standard library, and the app has no Swift package dependencies.

From the top of the repository:

1. `python3 -m unittest discover -s core/tests -t core` runs the core tests.
2. `swift test --package-path app` runs the app tests.
3. `swift build --package-path app` builds the app, and `bash scripts/bundle.sh` makes `build/Lifeline.app`.

Both test suites run on every push and pull request. The core tests run on Ubuntu and macOS, and the app tests on macOS.

## The test guard

A test must never reach a real keychain item. A test that missed its fake could overwrite your own Claude Code sign-in, so the suite has a guard.

1. `core/tests/__init__.py` turns the guard on for the whole suite.
2. While it is on, a call to `/usr/bin/security` or to the Security framework that is not faked raises `KeychainGuard` before it reaches the keychain. `KeychainGuard` is not an `Exception`, so no handler in Lifeline can swallow it.
3. Fake the runner (see `FakeSecurity` in `core/tests/test_slot.py`), or patch `subprocess.run`. Use a throwaway service name or keychain file, never `Claude Code-credentials` or the app's own service.
4. The few live keychain tests run only when you set `LIFELINE_LIVE_KEYCHAIN=1`, and only on a throwaway keychain file they create and delete.

## Code style

1. Keep changes small and in the style of the code around them. Reuse what exists before adding a function, file or setting.
2. Every sentence a person sees is built in the core, in the `*text.py` modules and the route replies. The app only draws what the core sends. Write plain English, one fact per sentence, with times on the 24-hour clock.
3. No outside packages in the core and no Swift package dependencies in the app.
4. Lifeline makes network calls only to Anthropic's own routes and to the paired computer. Keep it that way: no telemetry and no other hosts.
5. Never put a sign-in on a command line, in a log line or in an error message.

## Pull requests

1. Every pull request comes with tests. A fix comes with a test that fails without it.
2. Both suites pass.
3. Say in the description what changed and why, and what you could not test, such as a live keychain or a second computer.
