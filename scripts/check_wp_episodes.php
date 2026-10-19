<?php
require 'C:/laragon/www/wp-load.php';
$admins = get_users(['role' => 'administrator']);
foreach ($admins as $u) {
    echo $u->ID . " | " . $u->user_login . " | " . $u->user_email . PHP_EOL;
}
