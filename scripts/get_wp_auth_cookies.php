<?php
require 'C:/laragon/www/wp-load.php';

$user = get_user_by('id', 1);
if (!$user) {
    $user = get_user_by('login', 'test_user_a');
}
if (!$user) {
    echo json_encode(array('error' => 'No user found'));
    exit(1);
}

// Generate auth cookie
$expiration = time() + 3600 * 24 * 14;
$cookie = wp_generate_auth_cookie($user->ID, $expiration, 'logged_in');
$logged_in_cookie = LOGGED_IN_COOKIE;

$res = array(
    'cookies' => array(
        array(
            'name' => $logged_in_cookie,
            'value' => $cookie,
            'domain' => 'localhost',
            'path' => '/',
            'httpOnly' => true,
            'secure' => false,
        )
    ),
    'user_login' => $user->user_login,
    'user_id' => $user->ID
);

echo json_encode($res);
