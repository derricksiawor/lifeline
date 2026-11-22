let sampleState = """
{
  "version": 1,
  "at": "2026-11-19T15:41:00Z",
  "appearance": "dark",
  "mac": {
    "appearance": "dark", "view": "mac", "view_label": "This Mac", "sessions": "0 sessions open", "countdown": true,
    "icon_name": "Alex", "icon_pct": "100%", "icon_w": "100%", "icon_tone": "limit",
    "rows": [{"uuid": "11111111-aaaa-4aaa-8aaa-000000000001", "name": "Alex's Account", "pct": "100%", "week_pct": "Week 24%",
              "five_w": "100%", "fore_w": "0%", "week_w": "24%", "tone": "limit", "unused": false, "is_best": false,
              "is_active": true, "needs_signin": false, "where": "Used by this Mac. Back at 17:00."}],
    "hero_name": "Alex's Account", "hero_plan": "Max 5x",
    "head": "Switching this Mac to Team Account.", "sub": "In 5 seconds. Alex's Account is out until 17:00.",
    "hero_w": "100%", "fore_w": "0%", "hero_tone": "limit", "hero_used": "100% used", "hero_reset": "Back at 17:00",
    "week_w": "24%", "week_text": "This week: 24% used. It resets on Tuesday at 09:00.",
    "action_kind": "cancel", "action_text": "Cancel the switch", "action_target": "", "hint": ""
  },
  "dev": {
    "view": "dev", "view_label": "Studio", "countdown": false,
    "hero_name": "Work Account", "head": "Work Account lasts until it resets at 18:30.",
    "sub": "At this pace it ends the window 21% used.", "hero_tone": "calm",
    "action_kind": "none", "action_text": "", "action_target": "", "hint": "Lifeline switches only this Mac, so switch Studio there."
  },
  "accounts": [{
    "uuid": "11111111-aaaa-4aaa-8aaa-000000000001", "number": 1, "name": "Alex's Account", "short": "Alex",
    "email": "11111111@example.invalid", "plan": "Max 5x",
    "signed_in_at": null, "signin_ends": null, "five": 100.0, "week": 24.0,
    "five_reset": "2026-11-19T17:00:00Z", "week_reset": "2026-11-24T09:00:00Z",
    "forecast": {"at_reset_pct": 100.0, "runs_out_at": "2026-11-19T15:41:00Z", "pace_per_min": null, "basis": "none"},
    "needs_signin": false, "cannot_read": false, "paused_until": null, "reading_error": null,
    "reading_at": "2026-11-19T15:41:00Z", "in_use": ["mac"]
  }],
  "notifications": [{"id": "limit:11111111-aaaa-4aaa-8aaa-000000000001:2026-11-19T12:00:00+00:00", "kind": "limit",
                     "title": "Alex's Account is out until 17:00", "body": "Team Account has all of its 5 hours.",
                     "uuid": "11111111-aaaa-4aaa-8aaa-000000000001", "at": "2026-11-19T15:41:00Z"}],
  "jobs": [],
  "settings": {"auto_switch": true, "alerts": {"will_run_out": true, "limit": true, "back": true, "week": true, "signin_ends": true},
               "channels": {"mac": true}, "hide_name": false},
  "devbox": {"reachable": true, "account_uuid": "22222222-bbbb-4bbb-8bbb-000000000002"},
  "sessions": {"mac": {"open": 0, "behind": 0}, "dev": {"open": 0, "behind": 0}},
  "countdown": {"target_uuid": "44444444-dddd-4ddd-8ddd-000000000004", "ends_at": "2026-11-19T15:41:05Z", "seconds": 5.0}
}
"""
