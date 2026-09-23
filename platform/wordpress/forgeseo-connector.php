<?php
/**
 * Plugin Name: ForgeSEO Connector (narrow integration)
 * Description: Least-privilege REST endpoints for ForgeSEO SEO fields, operation mapping, and signed events.
 * Version: 0.4.0
 * Requires at least: 6.0
 * Requires PHP: 7.4
 * License: GPL-2.0-or-later
 * License URI: https://www.gnu.org/licenses/gpl-2.0.html
 */

/*
 * This file is original ForgeSEO code and is distributed under the GPL-2.0-or-
 * later license shown above. It deliberately uses WordPress public APIs only:
 * no arbitrary meta passthrough, plugin setting mutation, or remote fetch is
 * exposed by the connector routes.
 */

namespace ForgeSEO\Connector;

defined('ABSPATH') || exit;

final class REST {
    private const NAMESPACE = 'forgeseo/v1';
    private const OPERATION_META = '_forgeseo_operation_key';
    private const SEO_META = array(
        'title' => '_forgeseo_seo_title',
        'description' => '_forgeseo_seo_description',
    );
    private const PROVIDER_FILTERS = array(
        'yoast' => array(
            'title' => 'wpseo_title',
            'description' => 'wpseo_metadesc',
        ),
        'rank_math' => array(
            'title' => 'rank_math/frontend/title',
            'description' => 'rank_math/frontend/description',
        ),
    );

    public static function register(): void {
        register_rest_field(array('post','page','product'), 'forgeseo_seo', array(
            'get_callback' => static function ($post) {
                if (!current_user_can('edit_post', (int)$post['id'])) { return null; }
                return self::values((int)$post['id']);
            },
            'schema' => array('type'=>'object','context'=>array('edit'),'readonly'=>true),
        ));
        register_rest_route(self::NAMESPACE, '/capabilities', array(
            'methods' => \WP_REST_Server::READABLE,
            'callback' => array(__CLASS__, 'capabilities'),
            'permission_callback' => array(__CLASS__, 'can_edit_posts'),
        ));

        register_rest_route(self::NAMESPACE, '/posts/(?P<id>[\\d]+)/seo', array(
            array(
                'methods' => \WP_REST_Server::READABLE,
                'callback' => array(__CLASS__, 'get_seo'),
                'permission_callback' => array(__CLASS__, 'can_edit_post'),
            ),
            array(
                'methods' => \WP_REST_Server::CREATABLE,
                'callback' => array(__CLASS__, 'update_seo'),
                'permission_callback' => array(__CLASS__, 'can_edit_post'),
                'args' => self::seo_args(),
            ),
        ));

        register_rest_route(self::NAMESPACE, '/products/(?P<id>[\\d]+)/seo', array(
            array(
                'methods' => \WP_REST_Server::READABLE,
                'callback' => array(__CLASS__, 'get_seo'),
                'permission_callback' => array(__CLASS__, 'can_edit_product'),
            ),
            array(
                'methods' => \WP_REST_Server::CREATABLE,
                'callback' => array(__CLASS__, 'update_seo'),
                'permission_callback' => array(__CLASS__, 'can_edit_product'),
                'args' => self::seo_args(),
            ),
        ));

        register_rest_route(self::NAMESPACE, '/product-categories/(?P<id>[\\d]+)/seo', array(
            array(
                'methods' => \WP_REST_Server::READABLE,
                'callback' => array(__CLASS__, 'get_category_seo'),
                'permission_callback' => array(__CLASS__, 'can_edit_product_category'),
            ),
            array(
                'methods' => \WP_REST_Server::CREATABLE,
                'callback' => array(__CLASS__, 'update_category_seo'),
                'permission_callback' => array(__CLASS__, 'can_edit_product_category'),
                'args' => self::seo_args(),
            ),
        ));

        register_rest_route(self::NAMESPACE, '/operations/(?P<operation_key>[A-Za-z0-9._:-]+)', array(
            'methods' => \WP_REST_Server::READABLE,
            'callback' => array(__CLASS__, 'operation'),
            'permission_callback' => array(__CLASS__, 'can_edit_posts'),
        ));
    }

    public static function can_edit_posts(): bool {
        return current_user_can('edit_posts') || current_user_can('edit_products');
    }

    public static function can_edit_post(\WP_REST_Request $request): bool {
        $id = absint($request['id']);
        return in_array(get_post_type($id),array('post','page','product'),true) && current_user_can('edit_post', $id);
    }

    public static function can_edit_product(\WP_REST_Request $request): bool {
        $id = absint($request['id']);
        return get_post_type($id) === 'product' && current_user_can('edit_post', $id);
    }

    public static function can_edit_product_category(\WP_REST_Request $request): bool {
        $id = absint($request['id']);
        $term = get_term($id, 'product_cat');
        return !is_wp_error($term) && $term instanceof \WP_Term && current_user_can('manage_product_terms');
    }

    private static function values(int $id): array {
        $values = array();
        foreach(self::SEO_META as $name=>$key) { $values[$name]=(string)get_post_meta($id,$key,true); }
        return $values;
    }

    private static function term_values(int $id): array {
        $values = array();
        foreach(self::SEO_META as $name=>$key) { $values[$name]=(string)get_term_meta($id,$key,true); }
        return $values;
    }

    private static function detected_providers(): array {
        $providers = array();
        if (defined('WPSEO_VERSION')) { $providers[] = 'yoast'; }
        if (defined('RANK_MATH_VERSION')) { $providers[] = 'rank_math'; }
        return $providers;
    }

    private static function seo_provider(): string {
        $providers = self::detected_providers();
        if (count($providers) > 1) { return 'ambiguous'; }
        if (count($providers) === 1) { return $providers[0]; }
        return 'native';
    }

    private static function request_operation_key(): string {
        // The marker is valid for the current HTTP request only.  It is not
        // stored in post meta, so a later human edit can never inherit the
        // platform's correlation key and be mistaken for a feedback event.
        $value = isset($_SERVER['HTTP_X_FORGESEO_OPERATION_KEY'])
            ? wp_unslash($_SERVER['HTTP_X_FORGESEO_OPERATION_KEY'])
            : '';
        if (!is_string($value)) { return ''; }
        $value = trim($value);
        if ($value === '' || strlen($value) > 256 || !preg_match('/^[A-Za-z0-9._:-]+$/D', $value)) {
            return '';
        }
        return $value;
    }

    private static function seo_write_supported(): bool {
        return in_array(self::seo_provider(), array('native','yoast','rank_math'), true);
    }

    private static function managed_value(int $post_id, string $field, string $fallback): string {
        $value = (string)get_post_meta($post_id, self::SEO_META[$field], true);
        return $value !== '' ? $value : $fallback;
    }

    private static function managed_term_value(int $term_id, string $field, string $fallback): string {
        $value = (string)get_term_meta($term_id, self::SEO_META[$field], true);
        return $value !== '' ? $value : $fallback;
    }

    private static function is_product_category_archive(): bool {
        return function_exists('is_tax') && is_tax('product_cat');
    }

    private static function frontend_value(string $provider, string $field, string $fallback): string {
        if (self::seo_provider() !== $provider) {
            return $fallback;
        }
        $object_id = get_queried_object_id();
        if (!$object_id) { return $fallback; }
        if (is_singular(array('post','page','product'))) {
            return self::managed_value((int)$object_id, $field, $fallback);
        }
        if (self::is_product_category_archive()) {
            return self::managed_term_value((int)$object_id, $field, $fallback);
        }
        return $fallback;
    }

    private static function seo_args(): array {
        $args = array();
        foreach (array_keys(self::SEO_META) as $name) {
            $args[$name] = array(
                'required' => false,
                'type' => 'string',
                'sanitize_callback' => 'sanitize_text_field',
                'validate_callback' => static function ($value): bool {
                    return is_string($value) && strlen($value) <= 512;
                },
            );
        }
        return $args;
    }

    public static function capabilities(): \WP_REST_Response {
        $provider = self::seo_provider();
        $supported = self::seo_write_supported();
        $write_mode = 'unsupported';
        if ($provider === 'native') { $write_mode = 'native_meta'; }
        if (in_array($provider, array('yoast','rank_math'), true)) {
            $write_mode = 'documented_frontend_filters';
        }
        return new \WP_REST_Response(array(
            'namespace' => self::NAMESPACE,
            'seo_fields' => array_keys(self::SEO_META),
            'operation_mapping' => true,
            'webhooks' => true,
            'seo_provider' => $provider,
            'seo_write_supported' => $supported,
            'seo_write_mode' => $write_mode,
            'seo_restore_supported' => $supported,
            'direct_provider_meta_write' => false,
            'seo_resource_types' => array('posts','pages','products','product_categories'),
            'provider_filters' => self::PROVIDER_FILTERS,
        ), 200);
    }

    public static function get_seo(\WP_REST_Request $request): \WP_REST_Response {
        $post_id = absint($request['id']);
        $values = self::values($post_id);
        return new \WP_REST_Response(array(
            'resource_type' => get_post_type($post_id) ?: 'post',
            'id' => $post_id,
            'seo_provider' => self::seo_provider(),
            'seo' => $values,
        ), 200);
    }

    public static function update_seo(\WP_REST_Request $request) {
        $post_id = absint($request['id']);
        if (!self::seo_write_supported()) {
            return new \WP_Error('forgeseo_unsupported_seo_provider','SEO writes require exactly one supported provider or native WordPress metadata.',array('status'=>409));
        }
        $changed = array();
        foreach (self::SEO_META as $name => $meta_key) {
            if (!$request->has_param($name)) {
                continue;
            }
            $value = sanitize_text_field((string) $request->get_param($name));
            update_post_meta($post_id, $meta_key, wp_slash($value));
            $changed[$name] = $value;
        }
        return new \WP_REST_Response(array(
            'resource_type' => get_post_type($post_id) ?: 'post',
            'id' => $post_id,
            'seo_provider' => self::seo_provider(),
            'seo' => $changed,
        ), 200);
    }

    public static function get_category_seo(\WP_REST_Request $request): \WP_REST_Response {
        $term_id = absint($request['id']);
        return new \WP_REST_Response(array(
            'resource_type' => 'product_category',
            'id' => $term_id,
            'seo_provider' => self::seo_provider(),
            'seo' => self::term_values($term_id),
        ), 200);
    }

    public static function update_category_seo(\WP_REST_Request $request) {
        if (!self::seo_write_supported()) {
            return new \WP_Error('forgeseo_unsupported_seo_provider','SEO writes require exactly one supported provider or native WordPress metadata.',array('status'=>409));
        }
        $term_id = absint($request['id']);
        $changed = array();
        foreach (self::SEO_META as $name => $meta_key) {
            if (!$request->has_param($name)) {
                continue;
            }
            $value = sanitize_text_field((string) $request->get_param($name));
            update_term_meta($term_id, $meta_key, $value);
            $changed[$name] = $value;
        }
        return new \WP_REST_Response(array(
            'resource_type' => 'product_category',
            'id' => $term_id,
            'seo_provider' => self::seo_provider(),
            'seo' => $changed,
        ), 200);
    }

    public static function native_title(string $title): string {
        if (self::seo_provider() !== 'native') { return $title; }
        $object_id = get_queried_object_id();
        if (!$object_id) { return $title; }
        $values = is_singular(array('post','page','product'))
            ? self::values((int)$object_id)
            : (self::is_product_category_archive() ? self::term_values((int)$object_id) : array());
        if (!$values) { return $title; }
        return $values['title'] !== '' ? $values['title'] : $title;
    }

    public static function native_description(): void {
        if (self::seo_provider() !== 'native') { return; }
        $object_id = get_queried_object_id();
        if (!$object_id) { return; }
        $values = is_singular(array('post','page','product'))
            ? self::values((int)$object_id)
            : (self::is_product_category_archive() ? self::term_values((int)$object_id) : array());
        if (!$values) { return; }
        if ($values['description'] !== '') {
            echo '<meta name="description" content="'.esc_attr($values['description']).'" />'."\n";
        }
    }

    public static function yoast_title($title): string {
        return self::frontend_value('yoast', 'title', (string)$title);
    }

    public static function yoast_description($description): string {
        return self::frontend_value('yoast', 'description', (string)$description);
    }

    public static function rank_math_title($title): string {
        return self::frontend_value('rank_math', 'title', (string)$title);
    }

    public static function rank_math_description($description): string {
        return self::frontend_value('rank_math', 'description', (string)$description);
    }

    public static function changed(int $id, \WP_Post $post, bool $update): void {
        if (wp_is_post_revision($id) || wp_is_post_autosave($id) || !in_array($post->post_type,array('post','page','product'),true)) { return; }
        // Do not reuse the creation marker: later human edits must trigger fresh checks.
        $data = array('id'=>$id,'resource_type'=>$post->post_type,'updated'=>$update);
        $operation_key = self::request_operation_key();
        if ($operation_key !== '') {
            $data['operation_key'] = $operation_key;
        }
        self::send_event('post.changed',$data);
    }

    public static function changed_category(int $term_id, int $term_taxonomy_id = 0, array $args = array()): void {
        $term = get_term($term_id, 'product_cat');
        if (is_wp_error($term) || !$term instanceof \WP_Term) { return; }
        self::send_event('term.changed', array(
            'id' => (int)$term_id,
            'resource_type' => 'product_categories',
            'updated' => true,
        ));
    }

    public static function operation(\WP_REST_Request $request) {
        $operation_key = sanitize_text_field((string) $request['operation_key']);
        $posts = get_posts(array(
            'post_type' => 'post',
            'post_status' => 'any',
            'posts_per_page' => 2,
            'meta_key' => self::OPERATION_META,
            'meta_value' => $operation_key,
            'fields' => 'ids',
        ));
        if (count($posts) !== 1) {
            return new \WP_Error('forgeseo_operation_not_unique', 'Operation key was not uniquely mapped.', array('status' => 404));
        }
        return new \WP_REST_Response(array(
            'operation_key' => $operation_key,
            'resource_type' => 'post',
            'id' => (int) $posts[0],
        ), 200);
    }

    public static function remember_operation(\WP_Post $post, \WP_REST_Request $request, bool $creating): void {
        // Operation mappings identify platform-created drafts.  Do not write
        // the marker during ordinary REST updates; update requests use the
        // request-scoped header only to suppress their notification echo.
        if (!$creating) { return; }
        $operation_key = $request->get_header('X-ForgeSEO-Operation-Key');
        if (!is_string($operation_key) || $operation_key === '' || strlen($operation_key) > 256) {
            return;
        }
        if (!preg_match('/^[A-Za-z0-9._:-]+$/', $operation_key)) {
            return;
        }
        update_post_meta($post->ID, self::OPERATION_META, sanitize_text_field($operation_key));
        self::send_event('post.created', array(
            'id' => (int) $post->ID,
            'operation_key' => sanitize_text_field($operation_key),
            'creating' => (bool) $creating,
        ));
    }

    public static function register_settings(): void {
        register_setting('forgeseo_connector', 'forgeseo_connector_webhook_url', array(
            'type' => 'string',
            'sanitize_callback' => static function ($value): string {
                $value = trim((string) $value);
                if ($value === '') { return ''; }
                $url = esc_url_raw($value);
                $parts = wp_parse_url($url);
                if (!is_string($url) || !is_array($parts) || !in_array($parts['scheme'] ?? '', array('https', 'http'), true) || empty($parts['host'])) {
                    add_settings_error('forgeseo_connector_webhook_url', 'invalid_url', 'Enter a valid HTTP(S) webhook URL.');
                    return (string) get_option('forgeseo_connector_webhook_url', '');
                }
                return $url;
            },
        ));
        register_setting('forgeseo_connector', 'forgeseo_connector_webhook_secret', array(
            'type' => 'string',
            'sanitize_callback' => static function ($value): string {
                $value = trim((string) $value);
                if ($value === '') {
                    // A blank password field preserves the existing secret;
                    // clearing the URL is the explicit delivery stop switch.
                    return (string) get_option('forgeseo_connector_webhook_secret', '');
                }
                if (strlen($value) < 32 || preg_match('/[\x00-\x1F\x7F]/', $value)) {
                    add_settings_error('forgeseo_connector_webhook_secret', 'invalid_secret', 'The webhook secret must contain at least 32 non-control characters.');
                    return (string) get_option('forgeseo_connector_webhook_secret', '');
                }
                return $value;
            },
        ));
    }

    public static function add_settings_page(): void {
        add_options_page(
            'ForgeSEO Connector',
            'ForgeSEO Connector',
            'manage_options',
            'forgeseo-connector',
            array(__CLASS__, 'settings_page')
        );
    }

    public static function settings_page(): void {
        if (!current_user_can('manage_options')) { return; }
        ?>
        <div class="wrap">
            <h1>ForgeSEO Connector</h1>
            <p>Optional signed change notifications let ForgeSEO run a targeted audit soon after a page changes. Periodic polling remains active without this configuration.</p>
            <?php settings_errors(); ?>
            <form method="post" action="options.php">
                <?php settings_fields('forgeseo_connector'); ?>
                <table class="form-table" role="presentation">
                    <tr>
                        <th scope="row"><label for="forgeseo_connector_webhook_url">ForgeSEO webhook URL</label></th>
                        <td>
                            <input name="forgeseo_connector_webhook_url" id="forgeseo_connector_webhook_url" type="url" class="regular-text" value="<?php echo esc_attr((string) get_option('forgeseo_connector_webhook_url', '')); ?>" autocomplete="url" />
                            <p class="description">Use the site-specific URL shown in ForgeSEO Settings. Use HTTPS outside a local test environment.</p>
                        </td>
                    </tr>
                    <tr>
                        <th scope="row"><label for="forgeseo_connector_webhook_secret">Webhook secret</label></th>
                        <td>
                            <input name="forgeseo_connector_webhook_secret" id="forgeseo_connector_webhook_secret" type="password" class="regular-text" value="" autocomplete="new-password" />
                            <p class="description">At least 32 characters. Leave blank to preserve the stored secret; clear the URL above to disable notifications.</p>
                        </td>
                    </tr>
                </table>
                <?php submit_button('Save connector settings'); ?>
            </form>
        </div>
        <?php
    }

    public static function send_event(string $event, array $data): void {
        $webhook = get_option('forgeseo_connector_webhook_url', '');
        $secret = get_option('forgeseo_connector_webhook_secret', '');
        if (!is_string($webhook) || !is_string($secret) || $webhook === '' || $secret === '') {
            return;
        }
        $event_name = strtolower((string) preg_replace('/[^a-z0-9._-]/i', '', $event));
        if ($event_name === '') {
            return;
        }
        $payload = wp_json_encode(array(
            // WordPress's sanitize_key() removes dots; dotted event names are
            // part of the connector webhook contract (for example,
            // post.changed), so normalize the allowlisted characters directly.
            'event' => $event_name,
            'occurred_at' => gmdate('c'),
            'data' => $data,
        ));
        if (!is_string($payload)) {
            return;
        }
        $signature = hash_hmac('sha256', $payload, $secret);
        wp_safe_remote_post($webhook, array(
            'timeout' => 5,
            'blocking' => false,
            'headers' => array(
                'Content-Type' => 'application/json',
                'X-ForgeSEO-Signature' => $signature,
            ),
            'body' => $payload,
        ));
    }
}

add_action('rest_api_init', array(REST::class, 'register'));
add_action('rest_after_insert_post', array(REST::class, 'remember_operation'), 10, 3);
add_action('admin_init', array(REST::class, 'register_settings'));
add_action('admin_menu', array(REST::class, 'add_settings_page'));
add_action('save_post',array(REST::class,'changed'),10,3);
add_filter('pre_get_document_title',array(REST::class,'native_title'),99);
add_action('wp_head',array(REST::class,'native_description'),1);
add_filter('wpseo_title',array(REST::class,'yoast_title'),99);
add_filter('wpseo_metadesc',array(REST::class,'yoast_description'),99);
add_filter('rank_math/frontend/title',array(REST::class,'rank_math_title'),99);
add_filter('rank_math/frontend/description',array(REST::class,'rank_math_description'),99);
add_action('edited_product_cat',array(REST::class,'changed_category'),10,3);
