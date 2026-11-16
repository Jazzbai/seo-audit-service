=== Jazzb SEO Connector ===
Requires at least: 6.0
Requires PHP: 7.4
Stable tag: 0.5.0
License: GPL-2.0-or-later
License URI: https://www.gnu.org/licenses/gpl-2.0.html

Manage WordPress SEO titles and descriptions through Jazzb SEO without installing a separate SEO plugin.

== Description ==

The connector stores approved titles and descriptions in its own metadata fields. On a site without an SEO plugin, it uses WordPress's document-title and head hooks to render those values. Existing post/page titles, bodies and theme templates are preserved by the metadata endpoints.

Yoast and Rank Math are optional compatibility integrations. When exactly one is active, its documented output filters render the connector's saved values instead of adding a second description tag. Other SEO plugins and themes with custom head output need their own compatibility verification; this release does not certify every plugin or theme. Conflicting recognized providers disable metadata writes.

Writes require authenticated WordPress permissions for the specific resource. There is no arbitrary custom-field write endpoint. The platform checks its site policy, source revision, public HTML and rollback result separately.

Current managed SEO fields: document title and meta description. This connector does not yet provide a universal schema, robots, canonical, redirect or sitemap editor. WordPress's existing output continues for unmanaged fields.

== Installation ==

1. In WordPress, open Plugins > Add New Plugin > Upload Plugin.
2. Select the jazzb-seo-connector ZIP and activate Jazzb SEO Connector.
3. If replacing an older ForgeSEO Connector, use the existing plugin's replacement flow. The ZIP keeps the same internal folder and API identifiers so saved connections and metadata remain compatible.
4. Use a WordPress integration user with appropriate permissions and create a dedicated application password over HTTPS.
5. In Jazzb SEO, save the WordPress connection and run Test again. Metadata-write capability should now be available even when Yoast and Rank Math are absent.
6. Verify an approved metadata change and restoration on a test resource before enabling unattended writes. Retain the site's protected paths and existing publishing rules.

Settings > Jazzb SEO Connector displays the current metadata output mode. Webhook settings are optional; they are not required for metadata writes. Do not paste application passwords into webhook fields.

== Removal and rollback ==

Restore an individual change through the platform's revision-checked rollback. Deactivating this connector stops its output hooks and restores the theme or supported SEO provider's output. Saved connector metadata is retained for recovery. Do not delete an existing SEO plugin as part of setup.

== Changelog ==

= 0.5.0 =
* Rename the WordPress-facing interface to Jazzb SEO.
* Explicitly advertise that a separate SEO plugin is not required.
* Explain native and optional provider modes in settings.
* Preserve the existing native metadata implementation and legacy API/storage identifiers.
