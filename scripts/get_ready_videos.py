import subprocess

def main():
    cmd = [
        "ssh", "-o", "BatchMode=yes", "abood@127.0.0.1",
        "docker exec -i aboodvideo_postgres psql -U aboodvideo_user -d aboodvideo_db -c \"SELECT id, title, share_id FROM videos WHERE title LIKE '%Spirit_Cage%' LIMIT 2;\""
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    print(res.stdout)

if __name__ == "__main__":
    main()
