# Changelog

## v1.1.2

- Fix Telegram callback flow by adding a dedicated `@filter.callback_query()` bridge for `qbsub:*` actions.
- Normalize callback payload to `event.message_str` and forward it into `session_waiter` via `SessionWaiter.trigger`.
- For non-`qbsub` callback data, explicitly call `continue_event()` so other plugins can process the callback event.

## v1.1.1

- Refactor interactive flow back to AstrBot official `session_waiter` hook instead of plugin-local intercept state.
- Add a custom sender-based `SessionFilter` to adapt to AstrBot default filter changes and keep Telegram message/callback sessions consistent.
- Keep Telegram inline keyboard actions (`view/delete/tag/add/cancel`) working on the same waiter session.

## v1.1.0

- Fix interactive reply handling so pending `/qb` sessions are intercepted before they fall through to the LLM pipeline.
- Add Telegram inline keyboard support for `/qb` results.
- Keep text replies supported for non-Telegram platforms and as a fallback path.
