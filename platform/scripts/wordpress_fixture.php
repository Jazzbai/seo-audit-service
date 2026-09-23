<?php
// Explicitly isolated Docker fixture. Not a plugin and never deploy to a live site.
if (PHP_SAPI !== 'cli' || !file_exists('/fixture/setup.php')) { exit(1); }
$_SERVER['HTTP_HOST'] = 'fixture.test';
define('WP_INSTALLING', true);
define('FS_METHOD', 'direct');
require '/var/www/html/wp-load.php';
require_once ABSPATH . 'wp-admin/includes/upgrade.php';
require_once ABSPATH . 'wp-admin/includes/plugin.php';
$mode = $argv[1] ?? 'native';
$origin = $mode === 'woo' ? 'https://store.fixture.test' : 'https://wordpress.fixture.test';
if (!is_blog_installed()) {
    wp_install('Independent ForgeSEO Fixture', 'fixture_owner', 'fixture-owner@example.test', true, '', wp_generate_password(40, true, true));
}
update_option('home', $origin);
update_option('siteurl', $origin);
update_option('blog_public', 0);
update_option('permalink_structure', '/%postname%/');
global $wp_rewrite;
$wp_rewrite->set_permalink_structure('/%postname%/');
$wp_rewrite->flush_rules(true);
$user = get_user_by('login', 'fixture_owner');
wp_set_current_user($user->ID);
$commerce = array();
if ($mode === 'woo') {
    if (!file_exists(WP_PLUGIN_DIR . '/woocommerce/woocommerce.php')) {
        require_once ABSPATH . 'wp-admin/includes/file.php';
        require_once ABSPATH . 'wp-admin/includes/misc.php';
        require_once ABSPATH . 'wp-admin/includes/class-wp-upgrader.php';
        ob_start();
        $upgrader = new Plugin_Upgrader(new Automatic_Upgrader_Skin());
        // Fixed fixture version for repeatable compatibility tests, not a production recommendation.
        $installed = $upgrader->install('https://downloads.wordpress.org/plugin/woocommerce.10.0.4.zip');
        ob_end_clean();
        if (is_wp_error($installed) || !$installed) { fwrite(STDERR, 'WooCommerce fixture installation failed'); exit(4); }
    }
    ob_start();
    $activated = activate_plugin('woocommerce/woocommerce.php');
    ob_end_clean();
    if (is_wp_error($activated)) { fwrite(STDERR, 'WooCommerce fixture activation failed'); exit(5); }
    require_once WP_PLUGIN_DIR . '/woocommerce/woocommerce.php';
    if (!did_action('woocommerce_init')) { WC()->init(); }
    WC_Install::install();
    $product = new WC_Product_Simple();
    $product->set_name('Isolated repair supply '.wp_generate_uuid4());
    $product->set_description('<p>Catalog-confirmed repair supply.</p>');
    $product->set_regular_price('49.95');
    $product->set_sku('FIXTURE-'.wp_generate_uuid4());
    $product->set_manage_stock(true);
    $product->set_stock_quantity(11);
    $product->set_status('publish');
    $product_id = $product->save();
    $consumer_key = 'ck_'.bin2hex(random_bytes(20));
    $consumer_secret = 'cs_'.bin2hex(random_bytes(20));
    $wpdb->insert($wpdb->prefix.'woocommerce_api_keys', array('user_id'=>$user->ID,'description'=>'Isolated ForgeSEO fixture','permissions'=>'read_write','consumer_key'=>wc_api_hash($consumer_key),'consumer_secret'=>$consumer_secret,'truncated_key'=>substr($consumer_key,-7)));
    $commerce = array('consumer_key'=>$consumer_key,'consumer_secret'=>$consumer_secret,'product_id'=>$product_id);
}
if (isset($argv[2]) && $argv[2] === 'connector') {
    $directory = WP_PLUGIN_DIR . '/forgeseo-connector';
    wp_mkdir_p($directory);
    copy('/fixture/connector/forgeseo-connector.php', $directory . '/forgeseo-connector.php');
    $activated = activate_plugin('forgeseo-connector/forgeseo-connector.php');
    if (is_wp_error($activated)) { fwrite(STDERR, 'Connector activation failed'); exit(2); }
}
// Emit credentials only to the calling test process, which must not log stdout.
$password = WP_Application_Passwords::create_new_application_password($user->ID, array('name'=>'ForgeSEO isolated test '.wp_generate_uuid4()));
if (is_wp_error($password)) { fwrite(STDERR, 'Fixture application password failed'); exit(3); }
echo json_encode(array_merge(array('origin'=>$origin,'username'=>'fixture_owner','application_password'=>$password[0],'author_id'=>(string)$user->ID),$commerce));
