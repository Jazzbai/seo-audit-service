# Operator-approved internal HTTPS routes

The normal connector, public audit and rendered checks reject private DNS results. For an operator-owned WordPress host behind a gateway that cannot loop back through its public IP, the server can pin one public hostname to its known internal HTTPS address.

Set the backend runtime variable `TRUSTED_HTTPS_ROUTES` to a JSON object, for example:

```json
{"htownstarfamilydentistry.com":"10.0.1.14"}
```

This setting is server configuration, not a connection credential or a customer/site API setting. Leave it empty for ordinary public-only operation. Deploy the backend so API, workers and browser worker receive the same setting. The hostname mapping in host DNS is not sufficient by itself to authorize private access.

Only exact public DNS hostnames and RFC1918 IPv4 addresses are accepted, with a maximum of 16 routes. No wildcard, IP URL, localhost, link-local metadata address or arbitrary port is allowed. The route applies only to HTTPS on port 443. It fixes the socket destination, HTTP Host and TLS SNI; normal hostname/certificate verification remains enabled. Other hosts retain public DNS validation. Same-origin redirect and browser read-only restrictions still apply. A failed certificate must be repaired at the web server, never bypassed.

Before adding a route, verify the actual site's hosting destination, hostname certificate, and authenticated read access. Do not copy the H-Town address to unrelated sites or domains that happen to share a public IP.

To roll back application routing, remove the corresponding JSON entry and redeploy. Existing private host DNS will then be rejected again. Host DNS rollback is separate; consult the operator's guarded hosts-file rollback instructions. No database or WordPress content rollback is performed by this configuration change.
