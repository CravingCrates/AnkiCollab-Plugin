import importlib
import os
import sys
from pathlib import Path
import logging
import uuid
import re

import requests

import aqt
import anki
from aqt import mw

from .var_defs import API_BASE_URL

logger = logging.getLogger("ankicollab")

from .main import media_manager

try:
    from PIL import Image
    OPTIMIZATION_AVAILABLE = True
except ImportError as e:
    print(e)
    logger.warning("Pillow not available - image optimization disabled")
    OPTIMIZATION_AVAILABLE = False

# Configuration
WEBP_QUALITY = 85
JPEG_QUALITY = 85
PNG_COMPRESSION = 9
MAX_IMAGE_SIZE = 1920  # Maximum dimension for resizing

def is_allowed_filename(filename):
    """
    Check if a filename meets the required safety criteria.
    Implements the same logic as the Rust function is_allowed_extension.
    
    Args:
        filename: The filename to check
        
    Returns:
        bool: True if the filename is allowed, False otherwise
    """
    # Check length
    if not filename or len(filename) > 255 or len(filename) < 5:
        return False
    
    # Check for path separators or traversal patterns
    if '/' in filename or '\\' in filename or '..' in filename or filename.endswith('.') or filename.endswith(' '):
        return False
    
    # Check for empty after trimming
    if not filename.strip():
        return False
    
    # Check for valid characters
    allowed_chars = re.compile(r'^[a-zA-Z0-9\.\-_\s\(\)\+\,\%\&]*$')
    if not allowed_chars.match(filename):
        return False
    
    # Check first character is alphanumeric
    if not filename[0].isalnum():
        return False
    
    # Check contains at least one alphanumeric
    if not any(c.isalnum() for c in filename):
        return False
    
    # Check extension
    path = Path(filename)
    ext = path.suffix.lower().lstrip('.')
    if not ext:
        return False
    
    allowed_extensions = ['jpg', 'jpeg', 'png', 'gif', 'webp', 'svg', 'bmp', 'tif', 'tiff']
    return ext in allowed_extensions

def sanitize_filename(filename):
    """
    Sanitize a filename, replacing invalid filenames with random valid ones
    while preserving the extension if it's allowed.
    
    Args:
        filename: The original filename
        
    Returns:
        str: A valid filename (either the original or sanitized version)
    """
    if is_allowed_filename(filename):
        return filename
    
    # Extract the extension
    path = Path(filename)
    ext = path.suffix.lower().lstrip('.')
    
    # Check if extension is valid
    allowed_extensions = ['jpg', 'jpeg', 'png', 'gif', 'webp', 'svg', 'bmp', 'tif', 'tiff']
    if ext not in allowed_extensions:
        return None  # Invalid extension, can't sanitize
    
    # Generate a random name with the original extension
    random_id = str(uuid.uuid4())[:8]  # Use first 8 chars of UUID for readability
    new_filename = f"img_{random_id}.{ext}"
    
    logger.debug(f"Renamed invalid filename '{filename}' to '{new_filename}'")
    return new_filename

def can_optimize():
    """Check if optimization/pillow is available"""
    return OPTIMIZATION_AVAILABLE

def optimize_image(filepath, destination=None, convert_to_webp=True):
    """
    Optimize an image file to reduce size
    
    Args:
        filepath: Path to the original image
        destination: Optional destination path (creates one if None)
        convert_to_webp: Whether to convert to WebP format
        
    Returns:
        Path to the optimized file, or original if optimization failed
    """
    if not OPTIMIZATION_AVAILABLE:
        return filepath
        
    filepath = Path(filepath)
    
    if filepath.suffix.lower() not in ['.jpg', '.jpeg', '.png', '.gif', '.webp']:
        return filepath
        
    if destination is None:
        if convert_to_webp:
            destination = filepath.with_suffix('.webp')
        else:
            destination = filepath.with_name(f"{filepath.stem}_optimized{filepath.suffix}")
    
    # if destination already exists, we just use it bc we probably already optimized it
    des_path = Path(destination)
    if des_path.exists():
        return destination
    
    try:
        img = Image.open(filepath)
        
        if img.mode == 'RGBA' and not convert_to_webp:
            background = Image.new('RGB', img.size, (255, 255, 255))
            background.paste(img, mask=img.split()[3])
            img = background
        
        # Resize if too large
        if max(img.size) > MAX_IMAGE_SIZE:
            ratio = MAX_IMAGE_SIZE / max(img.size)
            new_size = (int(img.size[0] * ratio), int(img.size[1] * ratio))
            img = img.resize(new_size, Image.LANCZOS)
        
        if convert_to_webp:
            if filepath.suffix.lower() == '.gif':
                img.save(des_path, 'WEBP', quality=WEBP_QUALITY, save_all=True)
            else:
                img.save(des_path, 'WEBP', quality=WEBP_QUALITY)
        elif filepath.suffix.lower() in ['.jpg', '.jpeg']:
            img.save(des_path, 'JPEG', quality=JPEG_QUALITY, optimize=True)
        elif filepath.suffix.lower() == '.png':
            img.save(des_path, 'PNG', optimize=True, compress_level=PNG_COMPRESSION)
        else:
            img.save(des_path, optimize=True)
            
        if des_path.exists() and des_path.stat().st_size < filepath.stat().st_size:
            logger.debug(f"Optimized {filepath.name}: {filepath.stat().st_size} → {des_path.stat().st_size} bytes")
            return des_path
        else:
            logger.debug(f"Optimization didn't reduce size for {filepath.name}, using original")
            if des_path.exists():
                os.unlink(des_path)
            return filepath
            
    except Exception as e:
        logger.error(f"Error optimizing {filepath.name}: {str(e)}")
        # Clean up failed optimization attempt
        if des_path.exists():
            try:
                os.unlink(des_path)
            except:
                pass
        return filepath

async def optimize_media_file(filename, filepath_obj):
    """
    Optimize a media file if it's an image, converting to WebP if possible.
    
    Args:
        filename: Original filename
        filepath_obj: Path object to the file
        
    Returns:
        Tuple of (optimized filepath string, new filename, whether optimization occurred)
    """
    sanitized_name = sanitize_filename(filename)
    if sanitized_name is None:
        logger.warning(f"Skipping file with invalid extension: {filename}")
        return None, None, False
    
    if sanitized_name != filename:
        # Rename the file if it was sanitized
        new_filepath = filepath_obj.parent / sanitized_name
        try:
            filepath_obj.rename(new_filepath)
            filepath_obj = new_filepath
            filename = sanitized_name
            logger.info(f"Renamed file to: {sanitized_name}")
        except Exception as e:
            logger.error(f"Failed to rename invalid file {filename}: {str(e)}")
            return None, None, False
            
    if not can_optimize():
        return filepath_obj, filename, False
    
    filepath = str(filepath_obj)
    file_extension = filepath_obj.suffix.lower()
    is_image = file_extension in ['.jpg', '.jpeg', '.png', '.gif', '.webp']
    
    if is_image:
        
        optimized_filename = f"{filepath_obj.stem}.webp"
        optimized_filepath = os.path.join(os.path.dirname(filepath), optimized_filename)
        
        result_path = optimize_image(
            filepath, 
            destination=optimized_filepath,
            convert_to_webp=True
        )
        
        filepath = str(result_path)
        current_filename = Path(filepath).name
        return filepath, current_filename, True
    
    # Return original file info if no optimization occurred
    return filepath, filename, False

def sanitize_svg_files(svg_file_list):
    """
    Pre-sanitize SVG files
    
    Args:
        svg_file_list: List of tuples (filename, filepath_obj)
    
    Returns:
        Dictionary mapping original filenames to (sanitized_content, hash) tuples
    """
    MAX_BATCH_SIZE = 250  # Files per batch
    MAX_BATCH_BYTES = 1 * 1024 * 1024  # 1MB per batch
    
    svg_contents = []
    for filename, filepath_obj in svg_file_list:
        try:
            with open(filepath_obj, 'rb') as f:
                svg_content = f.read().decode('utf-8')
                if not svg_content:
                    logger.error(f"SVG {filename} is empty")
                    continue
                svg_contents.append((filename, svg_content))
        except Exception as e:
            logger.error(f"Error reading SVG {filename}: {str(e)}")
    
    # Split into properly sized batches
    batches = []
    current_batch = []
    current_batch_size = 0
    
    for filename, content in svg_contents:
        content_size = len(content.encode('utf-8'))
        
        # Check if adding this file would exceed batch limits
        if (len(current_batch) >= MAX_BATCH_SIZE or 
            current_batch_size + content_size > MAX_BATCH_BYTES) and current_batch:
            # Current batch is full, append it and start a new one
            batches.append(current_batch)
            current_batch = []
            current_batch_size = 0
        
        # Add file to current batch
        current_batch.append((filename, content))
        current_batch_size += content_size
    
    # Add the last batch if not empty
    if current_batch:
        batches.append(current_batch)
    
    logger.debug(f"Processing {len(svg_contents)} SVG files in {len(batches)} batches")
    
    # Process all batches
    sanitized_map = {}
    for batch_index, batch in enumerate(batches):
        logger.debug(f"Processing SVG batch {batch_index+1}/{len(batches)} with {len(batch)} files")
        
        payload = {
            "svg_files": [{"filename": f, "content": c} for f, c in batch]
        }
        
        try:
            response = requests.post(
                f"{API_BASE_URL}/media/sanitize/svg",
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=30
            )
            
            if response.status_code != 200:
                logger.error(f"Error sanitizing SVGs (batch {batch_index+1}): {response.text}")
                continue
                
            result = response.json()
            
            # Update our sanitized map with results from this batch
            for item in result["sanitized_files"]:
                sanitized_map[item["filename"]] = (item["content"], item["hash"])
                
        except Exception as e:
            logger.error(f"Exception during SVG sanitization (batch {batch_index+1}): {str(e)}")
    
    logger.debug(f"Successfully sanitized {len(sanitized_map)}/{len(svg_contents)} SVG files")
    return sanitized_map

async def optimize_svg_files(svg_file_list):
    """
    Optimize SVG files
    
    Args:
        svg_file_list: List of tuples (filename, filepath_obj)
        
    Returns:
        Dictionary mapping original filenames to (filepath, expected_hash, was_optimized)
    """    
    # Sanitize all SVGs in batches
    sanitized_map = sanitize_svg_files(svg_file_list)
    
    # Write optimized contents back and prepare result map
    result_map = {}
    for filename, filepath_obj in svg_file_list:
        if filename in sanitized_map:
            sanitized_content, exp_hash = sanitized_map[filename]
            try:
                # Write sanitized content back to file
                with open(filepath_obj, 'wb') as f:
                    f.write(sanitized_content.encode('utf-8'))
                
                result_map[filename] = (str(filepath_obj), exp_hash, True)
            except Exception as e:
                logger.error(f"Error writing optimized SVG {filename}: {str(e)}")
        else:
            # If sanitization failed or wasn't performed
            result_map[filename] = (str(filepath_obj), "", False)
    
    return result_map