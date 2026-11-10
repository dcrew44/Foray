# Reading employers' replies from Gmail

Foray can read your Gmail, and only read it, to find the employer's replies to applications
you sent:

- a **confirmation** ("we received your application") is recorded on the application as proof
  that it arrived, with the date and the subject line;
- a **rejection** or an **interview invitation** is pointed out on the match it is about, with
  one question: move this match to Rejected (or Interviewing)? Nothing moves until you press
  Move.

It is off until whoever runs the install gives it a Google OAuth client of their own (below).
There is no shared Foray client: your mail is read by your install, with your credentials, and
goes nowhere else.

> **Not yet run against Google.** This was built and tested against a stand-in for Google's
> servers, from Google's documentation. The first real connection is the first real test:
> [what to watch](#the-first-time-you-connect) lists what could differ.

## What it reads, what it keeps, what it never does

| | |
|---|---|
| **Asks Google for** | one scope, `https://www.googleapis.com/auth/gmail.readonly`. With it Foray cannot send, delete, label or change anything |
| **Searches** | the last 14 days of mail, outside the Promotions tab and not written by you, for the words an employer's reply uses (`QUERY` in `backend/apps/mail/google.py`). Every 15 minutes, while the worker runs |
| **Reads of each result** | the `From`, `Subject` and `Date` headers and the one-line preview Gmail shows (its "snippet"), by asking for `format=metadata`. Never the body, never an attachment, never who else it was sent to |
| **Keeps, for a confirmation, a rejection or an invitation** | Gmail's id for the message, when it arrived, the sender's display name and domain (not the address), the subject line, and the first 300 characters of the preview |
| **Keeps, for anything else** | Gmail's id and the date, so it is not read again. Nothing it said |
| **Forgets** | after 30 days, every message that was matched to none of your applications, whatever it was. What stays is mail about applications Foray knows you sent |
| **Shows to an AI model** | nothing. Messages are sorted by fixed rules (`backend/apps/mail/classify.py`), and matched to applications by the employer's name and domain (`backend/apps/mail/matching.py`) |
| **Stores of your Google access** | a refresh token and an access token, encrypted with a key derived from `SECRET_KEY` (`backend/apps/mail/crypto.py`). They are never shown, logged or returned by the API |

At most 50 new messages are read per check, newest first, so the first two weeks' backlog can
take a few checks to work through.

**How sure the sorting is.** The rules are deliberately cautious: a message they cannot place
is left alone. On made-up mail in wording the rules had not been tuned to, they recognised
about half of the confirmations, rejections and invitations, and (once two loose rules were
tightened) named nothing wrongly (`backend/tests/mail/test_classify.py` has the numbers and
how they were arrived at). So expect it to miss
some, and treat what it does find as a prompt, not a record of everything. One thing it cannot
do at all: Gmail's preview is the first couple of hundred characters, so a rejection that
spends them on thanks is not recognised.

## Set it up (once per install)

You need a Google account and about ten minutes in the
[Google Cloud console](https://console.cloud.google.com/). Google renames and moves these
screens from time to time; the names below are the ones to look for.

1. **Create a project** (the project picker at the top, then "New project"). Any name.
2. **Enable the Gmail API** for it: "APIs & Services", "Library", search for "Gmail API",
   "Enable". Without this every check fails, and Settings says the Gmail API is not enabled.
3. **Set up the consent screen** ("APIs & Services", "OAuth consent screen"; newer consoles
   call it "Google Auth Platform", with "Branding", "Audience" and "Data access" pages):
   - user type **External** (a personal `@gmail.com` account has no other choice);
   - an app name ("Foray" will do) and your email as the support and developer contact;
   - leave the publishing status at **Testing**;
   - under **Test users**, add the Gmail address you are going to connect. Only test users
     can connect while the app is in Testing;
   - under scopes ("Data access"), add `https://www.googleapis.com/auth/gmail.readonly`.
     Google lists it as a **restricted** scope. For an app published to the public that means
     a verification review and a security assessment; for an app in Testing with yourself as
     the test user it means only a warning screen when you connect.
4. **Create the client**: "APIs & Services", "Credentials", "Create credentials", "OAuth
   client ID", application type **Web application**. Under **Authorized redirect URIs** add
   exactly

   ```
   <PUBLIC_ORIGIN>/api/mail/google/callback
   ```

   with your install's `PUBLIC_ORIGIN` from `.env`: `http://localhost:3000/api/mail/google/callback`
   for a local install. Settings shows the exact address under Email while Gmail is not set
   up. Google accepts `http://` only for `localhost`; any other address has to be `https://`
   on a real domain name (not an IP address).
5. **Give the client to Foray.** Copy the client ID and the client secret into `.env`:

   ```
   GOOGLE_CLIENT_ID=1234567890-abc.apps.googleusercontent.com
   GOOGLE_CLIENT_SECRET=...
   ```

   Both or neither: Foray refuses to start with only one. Restart the api and the worker.
6. **Connect.** Settings, Email, "Connect Gmail". Google shows "Google hasn't verified this
   app" because the app is yours and in Testing: choose "Continue" (it may be under
   "Advanced"), then allow the read-only access. You come back to Settings, which names the
   mailbox. The worker makes the first check within a minute or two and one every 15 minutes
   after that.

## The seven-day limit, and how to avoid it

While the consent screen is in **Testing**, Google expires the refresh token it gives an
External app after **seven days**. Foray then stops checking, and Settings says "Google no
longer accepts Foray's access to this mailbox… Connect it again." Connecting again takes two
clicks and keeps everything already found, but it comes back every week.

Two ways out:

- **Publish the app** ("Audience", "Publish app", so the status reads "In production").
  Refresh tokens then last until you revoke them. You do not have to submit it for
  verification to use it yourself: an unverified app keeps showing the warning screen when
  you connect and is limited to 100 users, which is no limit for one person. Do not hand the
  client to other people on this footing.
- **Use a Google Workspace account** you administer, and make the app **Internal**. Internal
  apps need no verification and their tokens do not expire this way, but only accounts in
  your organisation can connect.

Google also revokes a refresh token when you change your Google password (for Gmail scopes),
when you remove the app under your account's third-party access, and after six months
without use. Each ends the same way: Settings asks you to connect again, and nothing is
retried until you do.

## The first time you connect

These rest on Google's documentation as read from memory, not on a real exchange. If one is
wrong, the symptom is next to it.

| What Foray assumes | If it is wrong |
|---|---|
| The consent page is `https://accounts.google.com/o/oauth2/v2/auth`, taking `client_id`, `redirect_uri`, `response_type=code`, `scope`, `access_type=offline`, `prompt=consent`, `state`, `code_challenge` and `code_challenge_method=S256` | Google shows an error page instead of the consent screen (it names the parameter) |
| A web-application client may send a PKCE `code_verifier` together with its client secret | Settings says "Google did not complete the connection" straight after you allow access |
| The token endpoint is `https://oauth2.googleapis.com/token`, form-encoded, and answers `access_token`, `expires_in`, `scope` (space-separated) and, the first time, `refresh_token` | the same sentence, or "Google gave Foray no way to keep reading…" when no refresh token came |
| A refresh token Google no longer honours is answered `400` with `"error": "invalid_grant"` | a dead token would show "Google refused the request" at every check instead of asking you to reconnect once |
| `GET https://gmail.googleapis.com/gmail/v1/users/me/profile` answers `emailAddress` | connecting fails after the consent screen; nothing is stored |
| `GET …/users/me/messages?q=…&maxResults=100` answers `messages: [{id}]` and `nextPageToken`, newest first, and understands `newer_than:14d`, `-category:promotions`, `-from:me`, `{a b "c d"}` | Settings shows "Gmail did not accept Foray's request", or checks succeed and find nothing |
| `GET …/users/me/messages/{id}?format=metadata&metadataHeaders=From&metadataHeaders=Subject&metadataHeaders=Date` answers `internalDate` (milliseconds), `snippet` (HTML-escaped) and `payload.headers` | messages are stored with no sender or subject, so nothing is recognised or matched |
| An expired access token is answered `401`; a disabled Gmail API `403` with reason `accessNotConfigured`; a rate limit `429` or `403` with `rateLimitExceeded` | the sentence in Settings is the general "Google refused…" one instead of the specific one |
| `POST https://oauth2.googleapis.com/revoke` with `token=` forgets the grant | Disconnect says Google could not be told; remove Foray at Google yourself (below) |
| Next's `/api/*` rewrite passes Django's redirect from the callback through to the browser | after allowing access you land on a blank or JSON page instead of Settings |

Worth doing once after the first connection: apply somewhere, mark the application sent in
Foray, and look at Settings, Email an hour later. "Found so far" should count the
confirmation, and the match page should say the employer confirmed it arrived.

## Disconnecting

Settings, Email, "Disconnect". Foray deletes the account, its tokens and every message it
read from the mailbox, and asks Google to forget the access. What stays is what has become
your own record: the date and subject already written on an application as proof it arrived,
and any stage you moved.

To be certain at Google's end as well, open your Google Account's
[third-party access](https://myaccount.google.com/connections) and remove the app there.
That works even with Foray switched off; its next check then asks you to reconnect.

## When connecting does not work

| Settings says | Look at |
|---|---|
| "That connection attempt expired, or did not start here" | You have to be using Foray at exactly `PUBLIC_ORIGIN` when you press Connect: the sign-in cookie for `http://127.0.0.1:3000` is not sent to `http://localhost:3000`, which is where Google sends you back. It also appears when more than ten minutes pass at Google's screen |
| "Google did not recognise this install's OAuth client" | `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`: both from the same "Web application" client, with no spaces or quotes |
| "The redirect URI registered with Google is not this install's" | The client's authorized redirect URI, character for character against the one Settings shows. (Google usually reports a mismatch on its own error page before you get this far) |
| "Foray was not given permission to read the mailbox" | The consent screen's scopes: `gmail.readonly` has to be there, and allowed when you connect |
| "The Gmail API is not enabled…" (after connecting) | Step 2 above, in the same project as the client |
| Google's page: "Access blocked" or "has not completed the Google verification process" | You are not listed as a test user (step 3), or the consent screen is "In production" and Google wants the warning acknowledged under "Advanced" |

## Operating notes

- **`SECRET_KEY` opens the stored tokens.** Change it and they can no longer be read:
  Settings asks you to connect again (nothing else is lost). A database backup without `.env`
  holds the tokens only in encrypted form; keep the two apart.
- **The worker does the reading.** No worker, no checks; `pnpm dev:all` and the `prod`
  profile both run one. The task is `mail.check`, on the system lane, at minutes 7, 22, 37 and
  52 of each hour.
- **A check that fails says why in Settings** and the next one tries again. Only a revoked or
  expired grant stops the checks, and only until you reconnect.
- **Requests**: connecting makes two requests to Google (the code exchange and one profile
  read), disconnecting one, and a check one listing per 100 results plus one per new message.
