# Production Checklist

Use this checklist before moving the cosmetology automation into live operation.

1. Fill `.env` with real Telegram, Avito, YCLIENTS, Codex, and allowed user settings.
2. Keep secrets out of git and logs.
3. Run Avito replies in preview mode first.
4. Review 10-20 real Avito conversations with the cosmetologist.
5. Update knowledge for services, prices, contraindications, preparation, aftercare, and handoff rules.
6. Test Telegram admin text commands.
7. Test Telegram admin voice commands.
8. Test YCLIENTS appointment create, move, cancel, and notes update in dry-run mode.
9. Enable live Avito sending only after review.
10. Enable live YCLIENTS mutations only after confirmation UX is accepted.
