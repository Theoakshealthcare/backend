from typing import Optional
import cloudinary
import cloudinary.uploader
from fastapi import UploadFile, HTTPException
from app.core.config import settings


class CloudinaryService:
    def __init__(self):
        if not all([settings.CLOUDINARY_CLOUD_NAME, settings.CLOUDINARY_API_KEY, settings.CLOUDINARY_API_SECRET]):
            raise ValueError("Cloudinary credentials are not properly configured")
        
        cloudinary.config(
            cloud_name=settings.CLOUDINARY_CLOUD_NAME,
            api_key=settings.CLOUDINARY_API_KEY,
            api_secret=settings.CLOUDINARY_API_SECRET
        )
    
    def upload_image(self, file: UploadFile, folder: str = "profile_images") -> str:
        try:
            allowed_types = ["image/jpeg", "image/png", "image/jpg"]
            if file.content_type not in allowed_types:
                raise HTTPException(
                    status_code=400, 
                    detail="Invalid file type. Only JPEG and PNG are allowed."
                )
            
            file_content = file.file.read()
            file.file.seek(0)
            
            result = cloudinary.uploader.upload(
                file_content,
                folder=folder,
                resource_type="image",
                format="jpg",
                quality="auto:good",
                fetch_format="auto"
            )
            
            return result.get("secure_url")
        
        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to upload image to Cloudinary: {str(e)}"
            )
    
    def delete_image(self, public_id: str) -> bool:
        try:
            result = cloudinary.uploader.destroy(public_id)
            return result.get("result") == "ok"
        except Exception as e:
            return False
    
    def get_public_id_from_url(self, url: str) -> Optional[str]:
        if not url:
            return None
        
        try:
            parts = url.split('/')
            if 'cloudinary.com' in url:
                upload_index = parts.index('upload') if 'upload' in parts else -1
                if upload_index != -1 and upload_index + 2 < len(parts):
                    filename_with_ext = parts[-1]
                    filename = filename_with_ext.rsplit('.', 1)[0]
                    folder = '/'.join(parts[upload_index + 2:-1])
                    return f"{folder}/{filename}" if folder else filename
            return None
        except (ValueError, IndexError):
            return None


cloudinary_service = CloudinaryService()