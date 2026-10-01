"""Stage 6I: the notification delivery boundary.

    task / monitoring outcome
            |
    task_notifications  (Stage 6H -- the source of truth)
            |
    NotificationDeliveryService   <- this package
            |
    NotificationAdapter(s)        <- channels: local today; Telegram, UI later

A notification is created only by `app.tasks.notifications.record_outcome`.
This package never creates one, never decides whether one should exist, and
never changes one. It reads an existing notification through the owner-scoped
`NotificationService`, reduces it to a closed, content-free payload, and hands
that to an adapter registered by name. Nothing more.
"""
