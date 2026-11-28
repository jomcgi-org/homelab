package main

import (
	"log/slog"
	"os"
	"strings"

	"github.com/jomcgi/homelab/projects/factory/discord"
	"github.com/jomcgi/homelab/projects/factory/discord/artifacts"
)

// wireShare turns on artifact share links (discord/share.go) when
// ARTIFACT_SHARE_KEY_DIR names the mounted bosun-artifacts-share-s3 Secret.
// The key is read-only on the bucket and is read at each link, so a rotated
// Secret applies without a restart.
func wireShare(log *slog.Logger, b *discord.Bridge) {
	dir := os.Getenv("ARTIFACT_SHARE_KEY_DIR")
	if dir == "" {
		return
	}
	base := strings.TrimRight(os.Getenv("ARTIFACT_BASE_URL"), "/")
	if !strings.HasPrefix(base, "https://") {
		log.Warn("share links off: ARTIFACT_BASE_URL is not https", "url", base)
		return
	}
	region := os.Getenv("ARTIFACT_SHARE_REGION")
	if region == "" {
		region = "garage"
	}
	bucket := os.Getenv("ARTIFACT_SHARE_BUCKET")
	if bucket == "" {
		bucket = "bosun-artifacts"
	}
	b.Share = &discord.Sharing{
		Signer:  &artifacts.Sharer{BaseURL: base, Region: region, Bucket: bucket, KeyDir: dir},
		BaseURL: base,
	}
	log.Info("share links on", "base", base, "bucket", bucket)
}
