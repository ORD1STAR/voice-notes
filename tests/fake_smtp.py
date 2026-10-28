"""A stand-in for smtplib.SMTP and smtplib.SMTP_SSL: it keeps the mails in memory and opens no connection."""


class Outbox:
    def __init__(self):
        self.mails = []  # each: {"host", "port", "tls", "login", "message"}
        self.connections = []

    def clear(self):
        self.mails.clear()
        self.connections.clear()

    def bodies(self):
        return [m["message"].get_content() for m in self.mails]


def make_fake(outbox: Outbox, ssl_class: bool = False):
    class FakeSMTP:
        def __init__(self, host="", port=0, timeout=None, context=None, **kw):
            self.host, self.port, self.tls, self.creds = host, port, ssl_class, None
            outbox.connections.append((host, port))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def ehlo(self):
            return 250, b"ok"

        def has_extn(self, name):
            return name.lower() == "starttls"

        def starttls(self, context=None):
            self.tls = True

        def login(self, user, password):
            self.creds = (user, password)

        def send_message(self, msg):
            outbox.mails.append({"host": self.host, "port": self.port, "tls": self.tls,
                                 "login": self.creds, "message": msg})

    return FakeSMTP
