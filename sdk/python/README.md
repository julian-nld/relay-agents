# Relay's Python SDK

Relay's agents from your own Python code: chats and code tasks, their answers
all at once or as they come, another agent mid-conversation, `/timed`, a code
task's changes; and your scripts, scheduled tasks, live services (Production
boxes) and notifications. Only the standard library; Python 3.9+.

Install it from your Relay (it serves the SDK; `/sdk/` has the command):

```sh
pip install http://<your-relay>:8420/sdk/relay_sdk-4.2.0-py3-none-any.whl
```

or from this folder: `pip install ~/relay/sdk/python`. Or install nothing: Relay's
**Scripts** page writes and runs scripts with it, signed in as you (no `login()`
there).

```python
from relay_sdk import *

user = login("me", "my password", url="http://my-server:8420", provider="claude-code", model="sonnet")
# or, without your password: an app password (Settings → Account), here or in $RELAY_APP_PASSWORD
user = connect("http://my-server:8420", app_password="relay_…", provider="claude-code", model="sonnet")
chat = convo.create(user, title="Site check", workdir="/home/me/projects/site", instructions="Answer in Dutch.")
message = chat.send("Run the tests and tell me what fails.")
print(message.response, message.elapsed_time, message.tools_called)

chat.switch(provider="opencode", model="opencode/big-pickle")
for chunk in chat.send("Fix the first one.", stream=True):
    print(chunk, end="", flush=True)

chat.send("Make all the tests pass.", timed=60)
for turn in chat.follow():
    print(turn.response)
chat.commit("Fix the tests")

for text in scripts.run("public/report.py", ["--short"]):           # your scripts, their output as it comes
    print(text, end="")
tasks.create("Daily report", "daily 08:00", script="public/report.py")
print(services.get("site").status()["text"])                         # your live services
notify("The report is ready", title="Report")                        # to your phone
```

RESL, Relay's Extended Standard Library, comes with it (`resl/`): `from resl import *` gives the SDK and
another model's judgement of an answer (`agent_refused(answer)`, `task_done`, `extract`, `best_of`, …).

A script that runs unattended (cron, another machine) is best signed in with an app password of its own: set
`RELAY_URL` and `RELAY_APP_PASSWORD` and it needs no `login()` or `connect()`. Revoking it in Settings → Account
stops only that script ([App passwords](../../docs/GUIDE.md#app-passwords)).

Behind a proxy with a sign-in of its own (Caddy's `basic_auth`), put its name and password in the address:
`RELAY_URL=https://name:password@relay.example`. The SDK sends them to the proxy and the app password to Relay
(`X-Relay-App-Password`), and keeps them out of its errors and links.

Relay's guide ([docs/GUIDE.md](../../docs/GUIDE.md#python-sdk)) has the rest: questions an agent asks, Relay's
tools per conversation, the VPN, retry, edit and fork, usage and limits,
scripts, tasks, services and notifications.
