<?php
require 'C:/laragon/www/wp-load.php';

$users = get_users();
foreach ($users as $u) {
    echo "USER: ID={$u->ID}, login={$u->user_login}, email={$u->user_email}" . PHP_EOL;
}
