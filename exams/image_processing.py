import cv2
import numpy as np
from PIL import Image

def align_perspective(image_bytes: bytes) -> bytes:
    """
    Given raw bytes of an image, uses OpenCV to normalize lighting, 
    remove glare, and optionally align perspective if a clear document
    boundary is detected.
    Returns bytes of the cleaned image.
    """
    # Convert bytes to numpy array
    nparr = np.frombuffer(image_bytes, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

    if img is None:
        raise ValueError("Could not decode image bytes")

    # Example light pre-processing to reduce glare and normalize
    # 1. Convert to LAB color space to equalize L channel
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    
    # CLAHE (Contrast Limited Adaptive Histogram Equalization)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8,8))
    cl = clahe.apply(l)
    
    # Merge back
    limg = cv2.merge((cl,a,b))
    final = cv2.cvtColor(limg, cv2.COLOR_LAB2BGR)
    
    # Re-encode to bytes
    success, encoded_img = cv2.imencode('.jpg', final)
    if not success:
        return image_bytes  # Fallback to original if encoding fails

    return encoded_img.tobytes()

def crop_image(image_bytes: bytes, bounding_box: dict) -> bytes:
    """
    Crop the image given by image_bytes using the bounding_box parsed.
    bounding_box should be a dict with normalized coordinates
    { 'minX': 0.1, 'minY': 0.2, 'maxX': 0.5, 'maxY': 0.7 }
    """
    nparr = np.frombuffer(image_bytes, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

    if img is None:
        raise ValueError("Could not decode image bytes")

    height, width, _ = img.shape
    
    min_x = int(min(bounding_box.get('minX', 0), 1.0) * width)
    min_y = int(min(bounding_box.get('minY', 0), 1.0) * height)
    max_x = int(min(bounding_box.get('maxX', 1.0), 1.0) * width)
    max_y = int(min(bounding_box.get('maxY', 1.0), 1.0) * height)

    cropped = img[min_y:max_y, min_x:max_x]
    
    success, encoded_img = cv2.imencode('.jpg', cropped)
    if not success:
        return image_bytes
        
    return encoded_img.tobytes()
