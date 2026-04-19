import os
import zipfile
import shutil

def create_release():
    """
    Bundles the application into a portable ZIP file.
    Includes core scripts, requirements, and empty configuration/data folders.
    Excludes large binaries and local user data to keep the release lightweight.
    """
    project_dir = os.path.dirname(os.path.abspath(__file__))
    release_name = "Video_Downloader_Portable.zip"
    release_path = os.path.join(project_dir, release_name)
    
    # Files and folders to include
    include = [
        "app.py",
        "Start.bat",
        "requirements.txt",
        "cookies.txt",           # Empty or user provided
        "config.json"
    ]
    
    print(f"Creating release: {release_name}...")
    
    with zipfile.ZipFile(release_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for file in include:
            file_path = os.path.join(project_dir, file)
            if os.path.exists(file_path):
                print(f"  Adding {file}...")
                zipf.write(file_path, file)
            else:
                print(f"  Skipping {file} (not found)")
                
        # Create an empty Downloads folder in the zip
        zipf.writestr('Downloads/', '')
        
    print(f"\nSuccess! Release bundle created at: {release_path}")
    print("You can now share this ZIP file with others.")

if __name__ == "__main__":
    create_release()
