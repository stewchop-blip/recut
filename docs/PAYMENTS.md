# Dormant Telegram Stars payment preparation

Sales are OFF by default (`PAYMENTS_ENABLED=false`). No buy button was added to the
existing menu. An empty price, credit count, terms or support contact also prevents
invoice creation. Existing video processing remains free and unchanged.

## Implemented

- `/buy` in private chat: immutable order snapshot, explicit acceptance of terms,
  server-side Stars price, single-user invoice with an opaque ID.
- `/terms`, `/paysupport`, `/balance`; support remains accessible if beta access is revoked.
- Pre-checkout verifies enabled sales, beta access, invoice age (1 hour), ownership,
  amount, currency and order state. It grants no credits.
- Successful Telegram receipt records a unique charge and credits in one transaction.
  Replays do not double-credit; receipts remain active after sales/access are disabled.
- Append-only credit ledger and persisted orders; no migration of existing tables.
- `/refund ORDER_ID` restricted to `PAYMENT_ADMIN_IDS` and private chats. Successful
  refunds debit the ledger exactly once. Uncertain network results retain
  `refund_pending` for reconciliation. Refunded-payment events are also handled.
- Polling preserves pending updates across deployment.

## Configuration (keep production disabled)

`PAYMENTS_ENABLED=false`, `PAYMENT_PRICE_STARS=0`, `PAYMENT_CREDITS=0`,
`PAYMENT_TERMS=` (approved terms, max 3000 characters),
`PAYMENT_SUPPORT=@stewchop`, `PAYMENT_ADMIN_IDS=` (separate from beta allowlist).

To test, use Telegram's dedicated test environment and a separate test database.
PAYMENTS_ENABLED is not a sandbox switch: enabling it on the production bot can
charge real Stars. No real payment or refund was performed during this change.

## Required before commercial activation

1. Choose prices, refund policy and final terms. Optional generation quotas now reserve
   and consume ledger credits for render callbacks (see GROWTH.md). One successful
   action, including a three-clip batch, costs one generation. Quotas remain disabled
   by default; test reservation recovery and concurrency on staging PostgreSQL before sales.
2. Test invoices, duplicates, expired invoices, changed prices, refunds, restart and
   receipt delivery against PostgreSQL and Telegram's test environment.
3. Implement/rehearse payment reconciliation using `getStarTransactions`: polling
   does not guarantee automatic retry if a handler fails after the update was read.
   An error is logged as `payment_reconciliation_required`, with no receipt internals.
4. Verify database backups and restore, support availability, and developer account
   eligibility to receive Stars rewards. No claim about country-specific withdrawal
   availability is made here.
5. Enable payments only after the above. Keep public access closed until workload
   isolation and the remaining security items in SECURITY_REVIEW.md are addressed.

Sources: https://core.telegram.org/bots/payments-stars
and https://core.telegram.org/bots/api#payments
