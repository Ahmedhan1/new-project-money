"""TikTok, Instagram and Facebook adapters: the documented request flow behind a "requires approved app" gate.

They are NOT claimed to work: none has run against a live account. `preflight` refuses unless the connected account
record says the owner's app was approved (`app_approved=True`) AND a token is present in the vault/env. Instagram and
Facebook have no private/draft API mode, so they additionally refuse without approval == 'approved_by_owner'.
TikTok: unaudited clients can post only privately (SELF_ONLY), which is what NEXUS requests unless approved public.
"""
from __future__ import annotations

from pathlib import Path

from .. import creds
from .base import Adapter, AdapterBlocked, PermanentError, Transport, classify

TT_INIT = "https://open.tiktokapis.com/v2/post/publish/video/init/"
TT_STATUS = "https://open.tiktokapis.com/v2/post/publish/status/fetch/"
GRAPH_VERSION = "v27.0"  # version shown on the video_insights reference page read 2026-11-12
GRAPH_FB = f"https://graph.facebook.com/{GRAPH_VERSION}"
RUPLOAD_IG = f"https://rupload.facebook.com/ig-api-upload/{GRAPH_VERSION}"


class GatedAdapter(Adapter):
    status = "requires_approved_app"
    review_steps: list[str] = []
    no_private_mode = False

    def __init__(self, transport=None):
        self.t = transport or Transport()

    def token(self, account: dict) -> str | None:
        return creds.get(account["secret_ref"])

    def preflight(self, manifest: dict, account: dict | None) -> None:
        if account is None or not account.get("app_approved"):
            raise AdapterBlocked("requires_approved_app",
                                 f"{self.platform}: publishing needs the owner's own developer app approved by the platform; "
                                 "NEXUS has not verified this flow against a live account", self.review_steps)
        if not self.token(account):
            raise AdapterBlocked("credentials_missing", f"{self.platform}: no access token at {account['secret_ref']} "
                                 f"(env {creds.env_name(account['secret_ref'])} or Account 1 vault)", self.review_steps)
        if self.no_private_mode and manifest.get("approval") != "approved_by_owner":
            raise AdapterBlocked("needs_owner_approval", f"{self.platform} has no private/draft API mode: publishing makes the post "
                                 "visible, so the owner must approve this manifest first", ["engine.approve(<manifest id>)"])
        if not Path(manifest["artifact"]["uri"].removeprefix("file://")).is_file():
            raise PermanentError("artifact file not found")


class TikTokAdapter(GatedAdapter):
    platform = "tiktok"
    review_steps = ["Register an app at https://developers.tiktok.com, add the Content Posting API product.",
                    "Get the video.publish scope approved; the creator authorises the app via Login Kit.",
                    "Until TikTok audits the app, every post is private (SELF_ONLY).",
                    "Store the user access token in the vault under the account's secret_ref, then connect_account(..., app_approved=True)."]

    def upload(self, manifest, account, ctx):
        path = Path(manifest["artifact"]["uri"].removeprefix("file://"))
        size = path.stat().st_size
        privacy = "PUBLIC_TO_EVERYONE" if (manifest["visibility"] == "public" and manifest.get("approval") == "approved_by_owner") else "SELF_ONLY"
        md = manifest["metadata"]
        body = {"post_info": {"title": md.get("caption") or md.get("title", ""), "privacy_level": privacy},
                "source_info": {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": size, "total_chunk_count": 1}}
        import json
        h = {"Authorization": f"Bearer {self.token(account)}", "Content-Type": "application/json; charset=UTF-8"}
        r = self.t.request("POST", TT_INIT, h, json.dumps(body).encode())
        classify(r, "tiktok init")
        data = r.json().get("data") or {}
        r2 = self.t.request("PUT", data["upload_url"], {"Content-Type": "video/mp4", "Content-Length": str(size),
                                                        "Content-Range": f"bytes 0-{size - 1}/{size}"}, (str(path), 0, size))
        classify(r2, "tiktok upload")
        return {"remote_id": data.get("publish_id"), "remote_status": "PROCESSING_UPLOAD", "privacy": privacy}

    def poll(self, remote_id, account):
        import json
        r = self.t.request("POST", TT_STATUS, {"Authorization": f"Bearer {self.token(account)}", "Content-Type": "application/json"},
                           json.dumps({"publish_id": remote_id}).encode())
        classify(r, "tiktok status")
        return {"remote_id": remote_id, **(r.json().get("data") or {})}


class InstagramAdapter(GatedAdapter):
    platform = "instagram_reels"
    no_private_mode = True
    review_steps = ["Use an Instagram professional (business/creator) account.",
                    "Create a Meta app with instagram_business_content_publish (Instagram Login) or instagram_content_publish + "
                    "pages_read_engagement (Facebook Login); obtain Advanced Access through Meta app review.",
                    "Resumable uploads need Facebook Login for Business; otherwise the video must be at a public URL (video_url).",
                    "Store the access token in the vault, set the account's external_id to the IG user id, connect_account(..., app_approved=True)."]

    def upload(self, manifest, account, ctx):
        path = Path(manifest["artifact"]["uri"].removeprefix("file://"))
        tok, ig = self.token(account), account.get("external_id")
        md = manifest["metadata"]
        params = {"media_type": "REELS", "upload_type": "resumable", "caption": md.get("caption", ""),
                  "share_to_feed": "true" if manifest["platform"] == "instagram_feed" else "false"}
        r = self.t.request("POST", f"{GRAPH_FB}/{ig}/media", {"Authorization": f"Bearer {tok}"}, b"", params=params)
        classify(r, "instagram container")
        cid = r.json()["id"]
        size = path.stat().st_size
        r2 = self.t.request("POST", f"{RUPLOAD_IG}/{cid}", {"Authorization": f"OAuth {tok}", "offset": "0", "file_size": str(size)},
                            (str(path), 0, size))
        classify(r2, "instagram rupload")
        st = self.t.request("GET", f"{GRAPH_FB}/{cid}", {"Authorization": f"Bearer {tok}"}, params={"fields": "status_code"})
        classify(st, "instagram status")
        code = st.json().get("status_code")
        if code != "FINISHED":
            return {"remote_id": cid, "remote_status": code, "note": "container not finished; poll then call media_publish"}
        pub = self.t.request("POST", f"{GRAPH_FB}/{ig}/media_publish", {"Authorization": f"Bearer {tok}"}, b"", params={"creation_id": cid})
        classify(pub, "instagram media_publish")
        return {"remote_id": pub.json().get("id"), "remote_status": "PUBLISHED", "container": cid}


class FacebookAdapter(GatedAdapter):
    platform = "facebook"
    no_private_mode = True
    review_steps = ["Facebook Pages only. Create a Meta app with pages_show_list, pages_read_engagement, pages_manage_posts "
                    "and pass Meta app review.", "Store the Page access token in the vault; external_id = Page id; "
                    "connect_account(..., app_approved=True)."]

    def upload(self, manifest, account, ctx):
        path = Path(manifest["artifact"]["uri"].removeprefix("file://"))
        md = manifest["metadata"]
        r = self.t.request("POST", f"{GRAPH_FB}/{account.get('external_id')}/videos",
                           {"Authorization": f"Bearer {self.token(account)}", "Content-Type": "application/octet-stream"},
                           (str(path), 0, path.stat().st_size), params={"title": md.get("title", ""), "description": md.get("description", "")})
        classify(r, "facebook videos")
        return {"remote_id": r.json().get("id"), "remote_status": "uploaded"}


class SnapchatManual(Adapter):
    platform = "snapchat_spotlight"
    status = "manual_export_only"

    def preflight(self, manifest, account):
        raise AdapterBlocked("manual_only", "Snapchat Spotlight has no public organic upload API (Public Profile API is "
                             "allowlist-only); upload the package manually",
                             [f"Open the package folder {manifest['metadata'].get('package_dir', '(see manifest)')} and follow MANUAL_UPLOAD.md",
                              "https://profile.snapchat.com > upload > 'Spotlight Snaps'"])
