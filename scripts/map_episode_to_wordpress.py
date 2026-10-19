"""Map an ABOODVIDEO Share ID to a WordPress Episode Post."""
import sys
import subprocess
from pathlib import Path

PHP_PATH = r"C:\laragon\bin\php\php-8.3.33-Win32-vs16-x64\php.exe"
WP_LOAD = r"C:/laragon/www/wp-load.php"

def map_episode(post_id: int, share_id: str) -> bool:
    php_code = f"""
    require '{WP_LOAD}';
    update_post_meta({post_id}, '_aboodvideo_video_id', '{share_id}');
    $val = get_post_meta({post_id}, '_aboodvideo_video_id', true);
    if ($val === '{share_id}') {{
        echo "MAPPED_OK:" . $val . "\\n";
        exit(0);
    }} else {{
        echo "MAPPED_FAIL\\n";
        exit(1);
    }}
    """
    res = subprocess.run([PHP_PATH, "-r", php_code], capture_output=True, text=True)
    print(res.stdout.strip())
    return "MAPPED_OK" in res.stdout

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python map_episode_to_wordpress.py <post_id> <share_id>")
        sys.exit(1)
    p_id = int(sys.argv[1])
    s_id = sys.argv[2].strip()
    success = map_episode(p_id, s_id)
    sys.exit(0 if success else 1)
