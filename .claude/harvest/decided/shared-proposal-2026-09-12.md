# Shared-memory proposals — embedthat, 2026-09-12

Harvested from 4 session digests (Track A) and `a8284b3..710e8a5` (Track B).
Nothing here has been written. `/memory-review` applies what is approved.

Format: `tier | action | the fact, as exact text to paste | target file | source | confidence`

## global

global | add | - **A site can block on the TLS stack, so "works on my box" proves nothing about the container.** Measured on embedthat 2026-09-10: TikTok's edge answered every yt-dlp request from the prod container with a 537-byte "Site Maintenance" page while the same yt-dlp version, from the same public IP, on the same host, got the normal 1462-byte WAF challenge page from a stock `python:3.12-slim-trixie` container. Deterministic, 5/5 both ways. The only measured difference was OpenSSL 3.0.18 (bookworm) vs 3.5.x (trixie). Newer client versions and `curl_cffi` impersonation both failed to move it. So when a fetch works from your shell and fails in a container, compare the two *response bodies* by length before touching code, and treat the base image as a suspect alongside IP, cookies, DNS and headers. | ~/.claude/memory/global.md | session 93da0596-00f4-4425-8db1-7b393332a32f | high

## Already recorded — proposed nothing

Two shared-tier facts surfaced in these digests were written to
`~/.claude/memory/global.md` by the originating sessions themselves, so they are
deduped rather than re-proposed:

- The `.lan` search-domain SSH failure mode (`ssh latitude` → *no route to host*;
  use `latitude.gg.ez`) — written by session d37319e2, under *Fleet SSH
  reachability*.
- The discriminator between the KZ SNI filter and router NAT-table exhaustion
  (bitmagnet's DHT crawler in `desktop-wsl`'s Docker fills the shared router's
  conntrack, starving *new* outbound flows LAN-wide; the boxes' own
  `nf_conntrack_count` reads near-idle because the exhausted table is the
  router's) — written by session abdd3f47, beside the byedpi bullet.

## host:g15

(none — nothing in these digests was specific to this machine rather than to the
repo or the fleet.)

---

# /cyphy:memory-review decision — 2026-09-12 (applied on g15)

The one row is applied.

APPLIED -> ~/.claude/memory/global.md (1): a site can block on the TLS STACK, so "works
on my box" proves nothing about the container — compare response-body lengths before
touching code, and treat the base image as a suspect.
