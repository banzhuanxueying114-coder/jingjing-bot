"""Copy this file to ``secret_patterns.py`` and fill in your own values.

``build.py`` scans the staged tree for every pattern listed here and refuses to
pack while anything matches. The real file is git-ignored so that publishing the
builder does not also publish what it is guarding against.
"""

import re

EXTRA_SECRETS = (
    ("the bot account", re.compile(r"PUT_YOUR_BOT_QQ_HERE")),
    ("my own account", re.compile(r"PUT_YOUR_QQ_HERE")),
    ("a whitelisted group", re.compile(r"PUT_A_GROUP_NUMBER_HERE")),
    ("a personal nick", re.compile(r"YOUR_NICK|YOUR_SECOND_NICK")),
    ("a build machine path", re.compile(r"[Dd]:[\\/]{1,2}your-workspace-name")),
    ("a private endpoint", re.compile(r"your-private-gateway\.example\.com")),
)
