<?php
// Explicitly isolated Docker fixture. It installs one free WordPress.org SEO
// plugin at a time and never targets a live site or a paid provider.
if (PHP_SAPI !== 'cli' || !file_exists('/fixture/setup.php')) { exit(1); }

$mode = $argv[1] ?? '';
$plugins = array(
    'yoast' => array(
        'host' => 'yoast.seo.fixture.test',
        'file' => 'wordpress-seo/wp-seo.php',
        'url' => 'https://downloads.wordpress.org/plugin/wordpress-seo.latest-stable.zip',
    ),
    'rank_math' => array(
        'host' => 'rank-math.seo.fixture.test',
        'file' => 'seo-by-rank-math/rank-math.php',
        'url' => 'https://downloads.wordpress.org/plugin/seo-by-rank-math.latest-stable.zip',
    ),
);
if (!isset($plugins[$mode])) {
    fwrite(STDERR, "SEO fixture mode must be yoast or rank_math\n");
    exit(2);
}

$host = $plugins[$mode]['host'];
$_SERVER['HTTP_HOST'] = $host;
$_SERVER['SERVER_NAME'] = $host;
$_SERVER['REQUEST_URI'] = '/';
$_SERVER['HTTPS'] = 'on';
define('WP_INSTALLING', true);
define('FS_METHOD', 'direct');
require '/var/www/html/wp-load.php';
require_once ABSPATH . 'wp-admin/includes/upgrade.php';
require_once ABSPATH . 'wp-admin/includes/plugin.php';

if (!is_blog_installed()) {
    wp_install(
        'Independent ForgeSEO SEO Fixture',
        'fixture_owner',
        'fixture-owner@example.test',
        true,
        '',
        wp_generate_password(40, true, true)
    );
}

$origin = 'https://' . $host;
update_option('home', $origin);
update_option('siteurl', $origin);
update_option('blog_public', 0);
update_option('permalink_structure', '/%postname%/');
if ($mode === 'rank_math') {
    // Rank Math's free plugin defers its REST/frontend hooks until its first
    // run is acknowledged. Keep this disposable fixture offline and skip only
    // that registration gate; no account or paid-provider setup is involved.
    update_option('rank_math_registration_skip', true);
}
global $wp_rewrite;
$wp_rewrite->set_permalink_structure('/%postname%/');
$wp_rewrite->flush_rules(true);

$user = get_user_by('login', 'fixture_owner');
if (!$user) {
    fwrite(STDERR, "SEO fixture owner was not created\n");
    exit(3);
}
wp_set_current_user($user->ID);

foreach ($plugins as $provider => $config) {
    if ($provider !== $mode && is_plugin_active($config['file'])) {
        deactivate_plugins($config['file'], true);
    }
}

$plugin = $plugins[$mode];
$plugin_path = WP_PLUGIN_DIR . '/' . $plugin['file'];
if (!file_exists($plugin_path)) {
    require_once ABSPATH . 'wp-admin/includes/file.php';
    require_once ABSPATH . 'wp-admin/includes/misc.php';
    require_once ABSPATH . 'wp-admin/includes/class-wp-upgrader.php';
    ob_start();
    $upgrader = new Plugin_Upgrader(new Automatic_Upgrader_Skin());
    $installed = $upgrader->install($plugin['url']);
    ob_end_clean();
    // Recent WordPress.org packages can extract under a versioned directory
    // (for example wordpress-seo.28.4). Normalize only this disposable
    // fixture to the canonical plugin slug before activation.
    if (!file_exists($plugin_path)) {
        $plugin_directory = dirname($plugin['file']);
        $expected_directory = WP_PLUGIN_DIR . '/' . $plugin_directory;
        $candidates = glob(WP_CONTENT_DIR . '/upgrade/' . $plugin_directory . '*', GLOB_ONLYDIR);
        if (is_array($candidates)) {
            foreach ($candidates as $candidate) {
                if (file_exists($candidate . '/' . basename($plugin['file']))) {
                    rename($candidate, $expected_directory);
                    break;
                }
                $nested_candidates = glob($candidate . '/*', GLOB_ONLYDIR);
                if (is_array($nested_candidates)) {
                    foreach ($nested_candidates as $nested_candidate) {
                        if (file_exists($nested_candidate . '/' . basename($plugin['file']))) {
                            rename($nested_candidate, $expected_directory);
                            break 2;
                        }
                    }
                }
            }
        }
    }
    if (is_wp_error($installed) || (!$installed && !file_exists($plugin_path)) || !file_exists($plugin_path)) {
        fwrite(STDERR, "SEO fixture plugin installation failed\n");
        exit(4);
    }
}

if (!is_plugin_active($plugin['file'])) {
    ob_start();
    $activated = activate_plugin($plugin['file']);
    ob_end_clean();
    if (is_wp_error($activated) || !is_plugin_active($plugin['file'])) {
        fwrite(STDERR, "SEO fixture plugin activation failed\n");
        exit(5);
    }
}

$connector_dir = WP_PLUGIN_DIR . '/forgeseo-connector';
wp_mkdir_p($connector_dir);
if (!copy('/fixture/connector/forgeseo-connector.php', $connector_dir . '/forgeseo-connector.php')) {
    fwrite(STDERR, "ForgeSEO connector copy failed\n");
    exit(6);
}
if (!is_plugin_active('forgeseo-connector/forgeseo-connector.php')) {
    ob_start();
    $connector = activate_plugin('forgeseo-connector/forgeseo-connector.php');
    ob_end_clean();
    if (is_wp_error($connector) || !is_plugin_active('forgeseo-connector/forgeseo-connector.php')) {
        fwrite(STDERR, "ForgeSEO connector activation failed\n");
        exit(7);
    }
}

if (!class_exists('WP_Application_Passwords')) {
    fwrite(STDERR, "WordPress application-password API is unavailable\n");
    exit(8);
}

$password = WP_Application_Passwords::create_new_application_password(
    $user->ID,
    array('name' => 'ForgeSEO isolated SEO test ' . wp_generate_uuid4())
);
if (is_wp_error($password)) {
    fwrite(STDERR, "SEO fixture application password failed\n");
    exit(9);
}

// The calling test captures this envelope in memory and never prints it.
echo wp_json_encode(array(
    'provider' => $mode,
    'origin' => $origin,
    'username' => 'fixture_owner',
    'application_password' => $password[0],
    'author_id' => (string)$user->ID,
));
