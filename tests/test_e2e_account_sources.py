"""`source_of` names the source `password_for` actually used.

`tests/e2e/_accounts.py` resolves the QA admin password from two places --
stdjflib's published per-server file and `$JMS_E2E_ADMIN_PASSWORD` -- and
then has a *second* function whose only job is to say which one it was, for
a failure message. The two are eleven lines apart and nothing tied them
together: reordering the resolver left its shadow testing the environment
first, so a run reading the file was told it had read the variable.

A diagnostic that names the wrong source is worse than none. It sends the
reader to check a password that was never consulted, which is the one thing
they would otherwise have got right.

Imported by path because `tests/e2e/` has no `__init__.py` (see
`tests/test_e2e_registry.py` for why, and for the precedent of reaching in
from here). `_accounts` imports nothing from `jellyfin_mpv_shim` and has no
side effects, so this costs no server and no mpv -- which is the point of
it living in the fast suite rather than beside the module it covers.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import importlib.util
import os
import unittest
from unittest import mock

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "e2e", "_accounts.py")


def _load():
    spec = importlib.util.spec_from_file_location("_e2e_accounts", _PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_accounts = _load()

ADDRESS = "http://127.0.0.1:8096"

#: Every shape the published file takes in practice: absent entirely (a run
#: from another machine), present with an admin password, and present
#: without one.
FACTS = (
    ("no published file", {}),
    ("file with an admin password", {"admin": "qa-admin",
                                     "admin_password": "from-the-file",
                                     "password": "stdjflib",
                                     "no_password": ["qa-nopassword"]}),
    ("file without an admin password", {"admin": "qa-admin",
                                        "password": "stdjflib",
                                        "no_password": ["qa-nopassword"]}),
)

ENVS = (("$%s set" % _accounts.ADMIN_PASSWORD_ENV, "from-the-environment"),
        ("$%s unset" % _accounts.ADMIN_PASSWORD_ENV, None))


class TheDiagnosticNamesTheSourceThatWasUsedTest(unittest.TestCase):
    """Across every combination, the password and the story about it agree."""

    def _expected_password_for_source(self, source, facts, env):
        """What the password must be, given what `source_of` claims.

        Deliberately keyed off the claim rather than re-deriving the
        resolution: re-deriving would agree with `password_for` by
        construction and could not see the two walking apart, which is the
        defect this module exists for.
        """
        if source == "$" + _accounts.ADMIN_PASSWORD_ENV:
            return env
        if source.startswith("a GUESS"):
            return _accounts.DEFAULT_PASSWORD
        # The remaining branch names the published file by path.
        return facts.get("admin_password")

    def test_the_admin_password_and_its_source_agree(self):
        for facts_label, facts in FACTS:
            for env_label, env in ENVS:
                with self.subTest(facts=facts_label, env=env_label):
                    environ = dict(os.environ)
                    environ.pop(_accounts.ADMIN_PASSWORD_ENV, None)
                    if env is not None:
                        environ[_accounts.ADMIN_PASSWORD_ENV] = env
                    with mock.patch.object(_accounts, "facts_for",
                                           return_value=facts), \
                            mock.patch.dict(os.environ, environ, clear=True):
                        password = _accounts.password_for(
                            _accounts.ADMIN_ACCOUNT, ADDRESS)
                        source = _accounts.source_of(
                            _accounts.ADMIN_ACCOUNT, ADDRESS)
                    self.assertEqual(
                        password,
                        self._expected_password_for_source(source, facts, env),
                        "source_of says the password came from %r, but "
                        "password_for returned %r, which is not what that "
                        "source holds" % (source, password))

    def test_the_file_wins_over_the_environment_in_both_functions(self):
        """The one combination the reorder got wrong, named on its own.

        Both are present and they differ -- which is the real case, because
        the variable is a whole-run fallback and the file is per server, so
        a two-server run has one variable and two different file passwords.
        """
        facts = dict(FACTS[1][1])
        environ = dict(os.environ)
        environ[_accounts.ADMIN_PASSWORD_ENV] = "from-the-environment"
        with mock.patch.object(_accounts, "facts_for", return_value=facts), \
                mock.patch.dict(os.environ, environ, clear=True):
            self.assertEqual(
                _accounts.password_for(_accounts.ADMIN_ACCOUNT, ADDRESS),
                "from-the-file")
            self.assertNotEqual(
                _accounts.source_of(_accounts.ADMIN_ACCOUNT, ADDRESS),
                "$" + _accounts.ADMIN_PASSWORD_ENV,
                "the file was used and the environment was named")

    def test_the_accounts_with_no_password_say_so_in_both(self):
        for facts_label, facts in FACTS:
            with self.subTest(facts=facts_label):
                with mock.patch.object(_accounts, "facts_for",
                                       return_value=facts):
                    for account in _accounts.NO_PASSWORD_ACCOUNTS:
                        self.assertEqual(
                            _accounts.password_for(account, ADDRESS), "")
                        self.assertIn(
                            "no password",
                            _accounts.source_of(account, ADDRESS))


if __name__ == "__main__":
    unittest.main()
