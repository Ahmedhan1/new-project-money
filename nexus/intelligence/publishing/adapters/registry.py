"""platform -> adapter instance."""
from __future__ import annotations

from .gated import FacebookAdapter, InstagramAdapter, SnapchatManual, TikTokAdapter
from .youtube import YouTubeAdapter

FACTORIES = {"youtube": YouTubeAdapter, "youtube_shorts": YouTubeAdapter, "tiktok": TikTokAdapter,
             "instagram_reels": InstagramAdapter, "instagram_feed": InstagramAdapter, "facebook": FacebookAdapter,
             "snapchat_spotlight": SnapchatManual}


def adapter_for(platform: str, transport=None):
    f = FACTORIES[platform]
    return f() if f is SnapchatManual else f(transport)
