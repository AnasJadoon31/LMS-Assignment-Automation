"""Classes: other sections' WhatsApp groups, run by their CR, under your rules.

A CR joins through a one-time invite you made, proves their number with a WhatsApp code and
signs in to the LMS; you approve the class. The class bot (a second WhatsApp number) then posts
that class's assignments to the groups the CR ticked and, if you allow it, answers the menu
there. It only ever reads the class's LMS: nothing is solved, uploaded or deleted for a class,
and nothing of yours is reachable from it.

policy.py   what each setting is, and who decides it (you, or the CR within your rules)
store.py    the class rows: create, approve, pause, remove, the log, blocks and usage
lms.py      the class's own LMS session, with or without a kept password
poller.py   reads each active class's LMS and queues its notices; sends the class outbox
menu.py     the menu, answered from the class's own courses and files
web.py      your Classes page, the CR's page and the invite sign-up
"""
