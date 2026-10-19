<?php
require 'C:/laragon/www/wp-load.php';

$ids = array(1806, 1807, 1808, 1809, 1810, 1811);
foreach ($ids as $id) {
    $p = get_post($id);
    $meta = get_post_meta($id, '_aboodvideo_video_id', true);
    $url = get_permalink($id);
    echo $id . " | " . ($p ? $p->post_title : "NOT FOUND") . " | meta: " . $meta . " | " . $url . PHP_EOL;
}
