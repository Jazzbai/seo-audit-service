# Microsoft 365 notifications: owner setup

This integration sends operational reports and a separately approved test email.
It does not read mail, publish articles, change WordPress, or create Microsoft
accounts. Keep ForgeSEO site/global publishing pauses on throughout setup.

## 1. Choose the mailbox and recipient

Use an existing Exchange Online tenant. A dedicated shared mailbox such as
`forgeseo@your-domain.example` keeps reports separate from personal mail. An alias
is only another address on an existing mailbox; this pilot uses the shared
mailbox's **primary address**, not alias-send behavior.

In Microsoft 365 admin, open **Teams & groups > Shared mailboxes**, create the
dedicated mailbox if needed, and give the human administrator appropriate access.
Keep direct sign-in for the shared account blocked. A basic shared mailbox up to
50 GB normally requires no additional mailbox license within an eligible Exchange
Online subscription; archiving, hold, larger storage and advanced features can
need licensing. Confirm the tenant's plan before provisioning anything billable.

Choose a test recipient whose inbox you can actually inspect. No test is sent
merely by saving a connection or checking authentication.

## 2. Register a dedicated application

In **Microsoft Entra > App registrations > New registration**:

1. Name it `ForgeSEO Notifications` and use this organization's accounts only.
2. Record the **Directory (tenant) ID** and **Application (client) ID**.
3. Create a client secret with an appropriate expiry. Store its **Value** privately
   in ForgeSEO, not its Secret ID. Keep a secure independent copy and expiry
   reminder. Never paste the value into chat, Git, reports or screenshots.
4. Open the application's **Enterprise application** and record its **Object ID**
   for the Exchange service-principal pointer. This is NOT the App registration
   Object ID.

Do **not** grant organization-wide Entra `Mail.Send` or `Mail.ReadWrite` application
permissions. This pilot uses **Exchange Online Application RBAC** instead. RBAC
grants and Entra grants add together: a narrow RBAC scope does not cancel a broad
Entra grant. ForgeSEO rejects recognizable broad Mail.Send/Mail.ReadWrite roles
in token diagnostics, but token inspection cannot prove the external RBAC scope.

Do not reuse an app that sends as other mailboxes; use this dedicated app so its
permission boundary is inspectable. No delegated mailbox login or mailbox
password is needed by ForgeSEO.

## 3. Grant access to only that mailbox

An Exchange administrator performs this step. The outline below is intentionally
not populated with guessed tenant or mailbox identifiers. Replace placeholders
only after confirming the dedicated app and mailbox. Existing objects must be
inspected before reuse; do not overwrite unrelated scopes or role assignments.

```powershell
Connect-ExchangeOnline

# App/client ID from App registrations; Object ID from Enterprise applications.
New-ServicePrincipal -AppId "APP-CLIENT-ID" -ObjectId "ENTERPRISE-APPLICATION-OBJECT-ID" -DisplayName "ForgeSEO Notifications"

New-ManagementScope -Name "ForgeSEO-Notification-Mailbox" -RecipientRestrictionFilter "PrimarySmtpAddress -eq 'forgeseo@your-domain.example'"

New-ManagementRoleAssignment -Name "ForgeSEO-Notification-MailSend" -Role "Application Mail.Send" -App "APP-CLIENT-ID" -CustomResourceScope "ForgeSEO-Notification-Mailbox"
```

Inspect the actual recipient scope and role assignments. Confirm the scope
contains **exactly the intended mailbox**, not an entire domain or organization.

```powershell
$forgeMailScope = Get-ManagementScope -Identity "ForgeSEO-Notification-Mailbox"
Get-Recipient -RecipientPreviewFilter $forgeMailScope.RecipientFilter | Select-Object DisplayName,PrimarySmtpAddress
Get-ManagementRoleAssignment -Identity "ForgeSEO-Notification-MailSend" | Format-List Name,Role,RoleAssigneeName,CustomResourceScope
Test-ServicePrincipalAuthorization -Identity "APP-CLIENT-ID" -Resource "forgeseo@your-domain.example"
Test-ServicePrincipalAuthorization -Identity "APP-CLIENT-ID" -Resource "AN-EXISTING-OUT-OF-SCOPE-MAILBOX"
```

The intended mailbox should be in scope for `Application Mail.Send`; the other
mailbox should be out of scope. These checks do not send messages. Also inspect
Entra API permissions separately: the Exchange test does not account for broad
Entra grants. Review **all** assignments for the dedicated app, not just one named
assignment, before attesting its scope. Permission propagation can take time.

The application's scope-review record is explicitly an **owner attestation**,
not an automatic Microsoft permission audit. Retain redacted command evidence
and identify who checked it. Do not include secrets or access tokens.

## 4. Configure ForgeSEO

Open the site, then **Settings > Connections > Microsoft 365**. Supply:

- Tenant ID, Client ID and client secret in the private credential inputs;
- the shared mailbox's primary sender address;
- the approved recipient list (maximum ten);
- weekly digests **off** for the initial test.

Credentials are encrypted by the existing site-connection store. The API never
returns the secret. Blank credential inputs preserve saved values; a revoked
connection requires replacement credentials. Do not enter these values into
the SMTP connection form.

Save and **Test connection**. This requests an access token and sends **no email**.
Authentication success is not proof of mailbox permission or delivery.

Record the mailbox-scope review only after completing the external checks above.
Both confirmations and meaningful evidence are required. The record is bound to
the exact saved connection/settings, expires after seven days, and is invalidated
by connection changes. The reviewing account must remain a ForgeSEO owner.

## 5. Send and confirm one test

1. Check the sender and recipients. Explicitly approve **one test notification**.
2. Wait for its job result. `accepted` means Graph returned HTTP 202; it does not
   mean that the recipient has received the email.
3. Open the recipient's inbox (also check junk/quarantine) and locate the message
   and operation identifier.
4. Confirm receipt in ForgeSEO only after actually receiving it. The system
   records this as an owner-confirmed recipient receipt, not a machine inbox read.

Duplicate submission with the same operation key reuses the job. If the provider
times out after sending begins, the outcome is unknown and **no automatic resend**
occurs. Inspect Exchange message trace/Sent Items before authorizing another test.
Revocation, changed configuration or expired scope review blocks queued work.

Weekly digests remain opt-in. Changing the toggle changes the saved connection
configuration and requires scope review again. A configured Graph connection
does not silently fall back to SMTP or send duplicate messages through both.

## Evidence required for the live-pilot preparation goal

- Fresh application authentication with no broad Entra send grant;
- single-mailbox Exchange role/scope evidence, including an out-of-scope check;
- an explicitly approved test with a durable job/acceptance record;
- confirmation that the approved recipient actually received that test;
- site and global publishing pauses still on, and zero pilot publications.

Offline tests prove software behavior, not the tenant configuration or receipt.

Sources:

- [Microsoft shared-mailbox requirements](https://learn.microsoft.com/en-us/microsoft-365/admin/email/about-shared-mailboxes?view=o365-worldwide)
- [Exchange Application RBAC and additive permissions](https://learn.microsoft.com/en-us/exchange/permissions-exo/application-rbac)
- [Microsoft Graph sendMail and HTTP 202 semantics](https://learn.microsoft.com/en-us/graph/api/user-sendmail?view=graph-rest-1.0)
- [Client credentials and application tokens](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-client-creds-grant-flow)
