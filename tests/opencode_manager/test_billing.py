from opencode_manager.bridge.billing import claude_billing, is_local_url, opencode_billing

FREE_PRICE = [{"input": 0, "output": 0, "cache": {"read": 0, "write": 0}}]
PRICE = [{"input": 1.25, "output": 10, "cache": {"read": 0.1, "write": 0}}]


def test_local_urls():
    for url in ("http://127.0.0.1:11434/v1", "http://localhost:8080", "http://192.168.1.5:11434/v1",
                "http://10.0.0.5/v1", "http://raspberrypi:11434", "http://pi.local/v1", "http://[::1]:1"):
        assert is_local_url(url), url
    for url in ("https://openrouter.ai/api/v1", "https://api.openai.com/v1", "http://8.8.8.8", "", None):
        assert not is_local_url(url), url


def test_claude_billing():
    assert claude_billing("none") == "subscription"
    assert claude_billing("ANTHROPIC_API_KEY") == "api"
    assert claude_billing(None) is None


def test_opencode_billing():
    oauth = {"connections": [{"type": "credential", "method": "oauth"}]}
    key = {"connections": [{"type": "credential", "method": "key"}]}
    chatgpt = {"id": "openai", "integrationID": "openai",
               "settings": {"baseURL": "https://chatgpt.com/backend-api/codex"}}
    zen = {"id": "opencode", "integrationID": "opencode", "settings": {"baseURL": "https://opencode.ai/zen/v1"}}
    ollama = {"id": "ollama", "settings": {"baseURL": "http://192.168.1.5:11434/v1"}}
    openrouter = {"id": "openrouter", "integrationID": "openrouter", "settings": {}}
    assert opencode_billing(chatgpt, oauth, {"cost": []}) == "subscription"
    assert opencode_billing(zen, key, {"cost": FREE_PRICE}) == "free"
    assert opencode_billing(zen, key, {"cost": PRICE}) == "api"
    assert opencode_billing(ollama, None, {"cost": []}) == "free"
    assert opencode_billing(openrouter, key, {"cost": PRICE}) == "api"
    assert opencode_billing(openrouter, key, None) == "api"
    assert opencode_billing(None, None, None) is None
