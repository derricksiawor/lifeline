import ast
import re
import unittest
from pathlib import Path

import lifeline

LOG_METHODS = {"debug", "info", "warning", "error", "exception", "critical"}
INTERNAL = re.compile(
    r"\b(jobs?|ids?|uuids?|slots?|services?|sockets?|traceback|stderr|stdout|exit code|hashed)\b"
    r"|\bbash\b|\.sh\b|\bsudo\b|launchctl|systemctl"
    r"|\blocks were disturbed\b|\bprivate(ly)?\b|\bincognito\b",
    re.IGNORECASE,
)
ERROR_NAME = re.compile(r"\b[A-Z][A-Za-z]*(Error|Exception)\b")
PACKAGE = Path(lifeline.__file__).parent


def _text(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(part.value if isinstance(part, ast.Constant) else "{}" for part in node.values)
    return None


def _prose(text):
    return bool(re.match(r"^[A-Z\"'{]", text)) and text.count(" ") >= 2 and re.search(r"[a-z]", text) is not None


def _logged(node, parents):
    while node in parents:
        node = parents[node]
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in LOG_METHODS:
            return True
        if isinstance(node, ast.Assign) and all(isinstance(t, ast.Name) and t.id.endswith("_LOG") for t in node.targets):
            return True
    return False


def sentences(source):
    tree = ast.parse(source)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(parents.get(node), ast.JoinedStr):
            continue
        text = _text(node)
        if text is not None and _prose(text) and not _logged(node, parents):
            found.append((node.lineno, text))
    return found


def faults(source):
    return [(line, text) for line, text in sentences(source) if INTERNAL.search(text) or ERROR_NAME.search(text)]


class PlainWordsTests(unittest.TestCase):
    def test_no_sentence_you_can_see_holds_an_internal_word(self):
        found = []
        for path in sorted(PACKAGE.glob("*.py")):
            found += [f"{path.name}:{line}: {text}" for line, text in faults(path.read_text())]
        self.assertEqual(found, [])

    def test_no_sentence_even_in_the_log_calls_the_other_computer_devbox(self):
        found = []
        for path in sorted(PACKAGE.glob("*.py")):
            tree = ast.parse(path.read_text())
            parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
            for node in ast.walk(tree):
                text = _text(node)
                if text and not isinstance(parents.get(node), ast.JoinedStr) and _prose(text) and "devbox" in text.lower():
                    found.append(f"{path.name}:{node.lineno}: {text}")
        self.assertEqual(found, [])

    def test_the_scan_catches_each_kind_of_fault_and_skips_the_log(self):
        source = "\n".join(
            [
                'A = "Lifeline could not finish the job."',
                'B = f"The mail server refused it ({type(e).__name__} SMTPAuthenticationError)."',
                'C = "Run bash devbox/install.sh --record on the Mac."',
                'E_LOG = "The service socket is closed."',
                'log.warning("The uuid %s has no slot.", x)',
                'F = "Lifeline switched this Mac to Work Account."',
                'I = "Lifeline opened the sign-in page in a private Safari window."',
            ]
        )
        self.assertEqual([line for line, _ in faults(source)], [1, 2, 3, 7])


if __name__ == "__main__":
    unittest.main()
