import pytest

from analyst.sanitize import defang_local_urls, is_firewall_page, is_local_url

FIREWALL_PAGE = (
    'Error: 403 - <div class="container">\n<img src="https://app.paradime.io/logo192.png" alt="Logo">'
    "<h1 class=\"title\">Bot or Not? Firewall's Robot Riddle</h1></div>"
)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8765/?thread=abc123",
        "http://localhost",
        "https://LOCALHOST:8501/history",
        "http://127.0.0.1:8765/?thread=abc",
        "http://[::1]:8765/",
        "http://10.0.0.12:8501/",
        "http://192.168.1.20/",
        "http://172.20.3.4/",
        "http://169.254.169.254/latest/meta-data",
        "http://0.0.0.0:8501/",
        "http://analyst.local/?thread=abc",
    ],
)
def test_local_and_private_links_are_local(url):
    assert is_local_url(url)


@pytest.mark.parametrize(
    "url",
    ["https://analyst.example.com/?thread=abc", "https://linear.app/acme/issue/DNA-42", "http://8.8.8.8/", "not a url"],
)
def test_public_links_are_not_local(url):
    assert not is_local_url(url)


def test_local_links_lose_their_scheme_and_everything_else_stays():
    text = (
        "Conversation: http://localhost:8765/?thread=abc123 (see also https://linear.app/acme/issue/DNA-42).\n"
        "```sql\nselect count(*) from wins -- winners only\n```\n<question>\nWho won?\n</question>"
    )

    assert defang_local_urls(text) == text.replace("http://localhost:8765", "localhost:8765")


def test_the_firewall_page_is_told_apart_from_api_errors():
    assert is_firewall_page(FIREWALL_PAGE)
    assert not is_firewall_page("Agent 'x' not found")
    assert not is_firewall_page("Error: 500 - Internal Server Error")
