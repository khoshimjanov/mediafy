from main import find_link, platform_for


def test_detects_instagram():
    assert find_link('Look https://www.instagram.com/reel/ABC/?utm_source=x') == (
        'https://www.instagram.com/reel/ABC/?utm_source=x', 'Instagram'
    )


def test_detects_youtube():
    assert platform_for('https://youtu.be/abc123') == 'YouTube'


def test_rejects_fake_domain():
    assert platform_for('https://instagram.com.evil.test/path') is None


def test_rejects_private_host():
    assert platform_for('http://localhost:8080/') is None
