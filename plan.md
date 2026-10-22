# Future Per-Visitor Session Plan

## Goal

Require requests to pass Cloudflare verification before the Site creates a user session, then add a per-visitor derived key without breaking Express sessions.

## Proposed Flow

1. Browser reaches Cloudflare.
2. Cloudflare performs WAF, Turnstile, or browser-challenge verification.
3. The Worker removes any client-supplied edge authentication headers.
4. The Worker sends a short-lived signed assertion containing:
   - random visitor ID
   - issued and expiry timestamps
   - request method/path
   - unique request ID
   - challenge result
5. The Site verifies the edge signature before trusting the visitor ID.
6. The Site derives a visitor-specific key:

   ```text
   visitorKey = HKDF-SHA256(
     masterSecret,
     salt = visitorId,
     info = "linga/user-session/v1",
     length = 32 bytes
   )