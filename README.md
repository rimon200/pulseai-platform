# pulseai-platform
AI platform for automated livestream clipping and content automation.

## YouTube long-form monitoring

YouTube support uses only the official YouTube Data API v3 for channel and
upload metadata. It never downloads media from YouTube. Generation requires an
operator-approved source configured for the creator and encrypted at rest.

Required configuration:

```text
YOUTUBE_INTEGRATION_ENABLED=false
YOUTUBE_API_KEY=
YOUTUBE_REQUEST_TIMEOUT_SECONDS=15
YOUTUBE_POLL_INTERVAL_MINUTES=15
YOUTUBE_MIN_VIDEO_DURATION_MINUTES=12
YOUTUBE_MAX_SOURCE_DURATION_HOURS=4
YOUTUBE_CLIPS_PER_VIDEO=3
YOUTUBE_MAX_CLIPS_PER_VIDEO=5
YOUTUBE_CLIP_MIN_SECONDS=45
YOUTUBE_CLIP_TARGET_SECONDS=90
YOUTUBE_CLIP_MAX_SECONDS=300
YOUTUBE_AUTOMATIC_GENERATION_ENABLED=false
YOUTUBE_MAX_VIDEOS_PER_CREATOR_PER_DAY=1
YOUTUBE_MAX_CLIPS_PER_CREATOR_PER_DAY=5
YOUTUBE_GLOBAL_MAX_CLIPS_PER_DAY=10
YOUTUBE_SOURCE_ENCRYPTION_KEY=
YOUTUBE_APPROVED_MEDIA_HOSTS=
```

`YOUTUBE_APPROVED_MEDIA_HOSTS` is a comma-separated HTTPS hostname allowlist.
Private-network addresses, localhost, data/file URLs, credentials embedded in
URLs, YouTube watch URLs, and redirects to unapproved hosts are rejected.
