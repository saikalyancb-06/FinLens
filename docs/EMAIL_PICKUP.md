# Email pickup — connecting any mailbox

The user types an email address and presses **Connect Mailbox**. Everything
after that is automatic.

## 1. Which sign-in page

`POST /email/route` (`app/mailbox/router.py`) decides, in this order:

| Step | Rule | Example |
|---|---|---|
| 1 | gmail.com / googlemail.com → Google; outlook.com, hotmail.*, live.*, msn.com → Microsoft | `a@gmail.com` |
| 2 | **MX record** on Google (`*.google.com`, `*.googlemail.com`) → Google; on `*.protection.outlook.com` → Microsoft | `kredo.in`, `bmsce.ac.in`, `rvce.edu.in` → Google; `iisc.ac.in`, `wipro.com` → Microsoft |
| 3 | MX names another provider (Zoho, Yahoo, iCloud, Rediff, GoDaddy, …) → IMAP | `zoho.com` |
| 4 | Otherwise (filtering gateway such as Mimecast / Trend Micro, or the company's own server): **SPF** naming only Google or only Microsoft decides | |
| 5 | Otherwise **Microsoft 365 tenant lookup** (Managed / Federated) → Microsoft | `infosys.com`, `tcs.com` |
| 6 | Otherwise IMAP with an application password — and the form still offers **Sign in with Google / Microsoft**, because only the provider's page knows for certain | |

MX wins over the tenant lookup on purpose: kredo.in has an idle Microsoft
tenant while its mail is on Google.

## 2. The sign-in

* The sign-in window is opened in the click itself (browsers block popups
  opened later); if it is blocked anyway, the page shows an **Open Google
  sign-in** button.
* The provider's page opens **on the typed address** (`login_hint`).
* Google: `gmail.readonly` + `userinfo.email`, offline access, consent prompt.
  Microsoft: `Mail.Read`, `User.Read`, `offline_access`, tenant `common`
  (personal and work/school accounts).

## 3. The callback (`/email/oauth/callback`, `/email/oauth/microsoft/callback`)

1. The one-time state is checked (CSRF) and tied back to the user.
2. The code is exchanged; the authorised address is read from the provider.
3. Refused if the user unticked **Read your email** on Google's consent page.
4. Refused, with the reason, if the account has **no mailbox** (a Google
   account for an address whose mail is on Microsoft, or the reverse).
5. The connection is saved (tokens encrypted) and the **first scan starts at
   once** (`MAILBOX_SCAN_ON_CONNECT`, default on).
6. The outcome is recorded on the state; the app polls
   `GET /email/oauth/result?state=…` — so it works even when the popup is cut
   off from the app tab — and then follows the scan to the end.

One scan per mailbox runs at a time; a second press of **Scan** waits for
the first instead of importing the same statements twice.

## 4. Server setup (once)

**Google Cloud Console** → APIs & Services:

* Enable the **Gmail API**.
* OAuth client (Web application) → Authorised redirect URI:
  `https://<your-domain>/email/oauth/callback` (and
  `http://localhost:8000/email/oauth/callback` for local use).
* OAuth consent screen: add `gmail.readonly`. While the app is in **Testing**,
  only the listed **test users** can sign in and their sign-in lasts 7 days;
  for anyone else (any company domain) the app must be **published and
  verified** — `gmail.readonly` is a restricted scope, which needs Google's
  security assessment (CASA).
* A Google Workspace admin (e.g. at bmsce.ac.in) can block unverified apps for
  their domain; the user then sees "administrator has restricted access".

**Microsoft Entra** → App registrations:

* Supported account types: **any organisational directory and personal
  Microsoft accounts**.
* Redirect URI (Web): `https://<your-domain>/email/oauth/microsoft/callback`.
* API permissions (delegated): `Mail.Read`, `User.Read`, `offline_access`.
* Some organisations require an admin to approve the app once.

**Environment**: `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`,
`MICROSOFT_CLIENT_ID`, `MICROSOFT_CLIENT_SECRET`. Set
`GOOGLE_REDIRECT_URI` / `MICROSOFT_REDIRECT_URI` to the exact URIs registered
above; when they are set they are used for both legs of the flow (a localhost
value is ignored on a public server). When unset, the URI is built from the
request's host and `X-Forwarded-Proto`.

Tests: `tests/test_email_connect_flow.py`.
