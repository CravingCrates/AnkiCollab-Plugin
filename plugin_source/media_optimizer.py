import importlib
import os
import sys
from pathlib import Path
import logging
import shutil
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
    from PIL import Image, UnidentifiedImageError
    # Newer Pillow versions use Resampling enums
    try:
        PIL_Resampling = Image.Resampling
    except AttributeError:
        # Fallback for older Pillow versions
        PIL_Resampling = Image
    OPTIMIZATION_AVAILABLE = True
except ImportError as e:
    print(e)
    logger.warning("Pillow not available - image optimization disabled")
    OPTIMIZATION_AVAILABLE = False
    PIL_Resampling = None # Define it as None if Pillow isn't available

# Configuration
WEBP_QUALITY = 85
JPEG_QUALITY = 85
PNG_COMPRESSION = 9
MAX_IMAGE_SIZE = 1920  # Maximum dimension for resizing

OPTIMIZABLE_INPUT_EXTENSIONS = [
        '.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.tif', '.tiff'
        # We do not optimize svgs, or audio files
    ]

ALLOWED_EXTENSIONS = ['jpg', 'jpeg', 'png', 'gif', 'webp', 'svg', 'bmp', 'tif', 'tiff', 'mp3', 'ogg']
   
FORMAT_TO_EXTENSION = { # the bugfix we use this for does not apply to audio files so we don't need to include them
    'JPEG': '.jpg', # Standardize to .jpg
    'PNG': '.png',
    'GIF': '.gif',
    'WEBP': '.webp',
    'BMP': '.bmp',
    'TIFF': '.tif', # Standardize to .tif
}

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
    
    # Check for valid characters (ASCII only)
    # Replace \s with a literal space to avoid matching unicode whitespace like U+202F
    allowed_chars = re.compile(r'^[a-zA-Z0-9\.\-_ \(\)\+\,\%\&]*$')
    if not allowed_chars.match(filename):
        # Additionally check if all characters are within the basic ASCII range
        # This catches characters not explicitly listed but still potentially problematic
        if not all(ord(c) < 128 for c in filename):
            return False
        # If the regex failed but all chars are ASCII, it means an unlisted but valid ASCII char was used.
        # This case *shouldn't* happen with the current regex, but added as a safeguard.
        # If it *does* fail the regex but *is* all ASCII, we still reject it based on the regex.
        return False

    # Check first character is alphanumeric
    if not filename[0].isalnum():
        return False
    
    # Check contains at least one alphanumeric
    if not any(c.isalnum() for c in filename):
        return False
        
    return True

def sanitize_filename(filename):
    """
    Sanitize a filename, replacing invalid filenames with random valid ones
    while preserving the extension if it's allowed.
    
    Args:
        filename: The original filename
        
    Returns:
        str: A valid filename (either the original or sanitized version)
    """    
    # Extract the extension
    if not filename:
        return False
    
    path = Path(filename)
    ext = path.suffix.lower().lstrip('.')
    
    if not ext:
        return False
    
    # Check if extension is valid
    if ext not in ALLOWED_EXTENSIONS:
        return None  # Invalid extension, can't sanitize
    
    if is_allowed_filename(filename):
        return filename
    
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
        Tuple[Path, bool]:
            - Path: The path to the file that should be used (original or optimized/converted).
            - bool: True if the intended `destination` file was successfully created and is the returned path, False otherwise.
    """
    if not OPTIMIZATION_AVAILABLE:
        logger.warning("Optimization skipped: Pillow not available.")
        return Path(filepath), False
        
    filepath = Path(filepath)
    
    if not filepath.is_file():
        logger.error(f"Source file not found or is not a file: {filepath}")
        return filepath, False
    try:
        original_size = filepath.stat().st_size
    except OSError as e:
        logger.error(f"Could not get stats for source file {filepath}: {e}")
        return filepath, False
    
    if filepath.suffix.lower() not in OPTIMIZABLE_INPUT_EXTENSIONS:
        logger.debug(f"Skipping optimization for file type Pillow likely doesn't handle: {filepath.name}")
        return filepath, False

    des_path = Path(destination) if destination else None

    if not des_path:
         logger.error("Optimize_image called without a destination path.")
         return filepath, False
     
    # Avoid redundant work if source and destination are identical and no format change is intended
    # (convert_to_webp=True implies format change unless source is already webp)
    will_change_format = (
        convert_to_webp and filepath.suffix.lower() != '.webp'
    ) or (
        not convert_to_webp and des_path.suffix.lower() != filepath.suffix.lower() # Explicit format change goal
    )

    if des_path == filepath and not will_change_format:
         logger.debug(f"Skipping optimization: Destination same as source, and no format change intended for {filepath.name}.")
         # Technically destination exists, but wasn't *created* by this optimization run.
         return filepath, False
     
    try:
        with Image.open(filepath) as img:
            original_format = img.format
            original_mode = img.mode
            resized = False

            # Convert RGBA to RGB with white background if saving to JPEG
            if img.mode in ('RGBA', 'LA') and (not convert_to_webp and des_path.suffix.lower() not in ['.png', '.webp', '.tif']):
                logger.debug(f"Converting {img.mode} to RGB for {filepath.name} before saving to {des_path.suffix}")
                try:
                    # Create a white background and paste the image onto it
                    background = Image.new('RGB', img.size, (255, 255, 255))
                    # Paste using alpha channel as mask if available
                    alpha_channel = img.getchannel('A')
                    background.paste(img, mask=alpha_channel)
                    img = background # Replace img with the RGB version
                except Exception as bg_err:
                    logger.warning(f"Could not apply alpha mask for {filepath.name}, converting directly to RGB: {bg_err}")
                    img = img.convert('RGB')


            # Resize if too large
            if max(img.size) > MAX_IMAGE_SIZE:
                ratio = MAX_IMAGE_SIZE / max(img.size)
                new_size = (int(img.size[0] * ratio), int(img.size[1] * ratio))
                logger.debug(f"Resizing {filepath.name} from {img.size} to {new_size}")
                resample_filter = PIL_Resampling.LANCZOS if PIL_Resampling else Image.LANCZOS
                img = img.resize(new_size, resample=resample_filter)
                resized = True

            save_options = {}
            target_format = None

            if convert_to_webp:
                target_format = 'WEBP'
                save_options['quality'] = WEBP_QUALITY
                if original_format == 'GIF' and getattr(img, 'is_animated', False):
                    try:
                        save_options['save_all'] = True
                        img.seek(0)
                        save_options['duration'] = img.info.get('duration', 100)
                        save_options['loop'] = img.info.get('loop', 0)
                        logger.debug(f"Attempting animated WebP conversion for {filepath.name}")
                    except Exception as anim_err:
                         logger.warning(f"Could not fully configure animated WebP save for {filepath.name}: {anim_err}. Saving first frame.")
                         if 'save_all' in save_options: del save_options['save_all']
                         img.seek(0) # Ensure first frame is selected
                         
            elif des_path.suffix.lower() in ['.jpg', '.jpeg']:
                target_format = 'JPEG'
                save_options['quality'] = JPEG_QUALITY
                save_options['optimize'] = True
            elif des_path.suffix.lower() == '.png':
                target_format = 'PNG'
                save_options['optimize'] = True
                save_options['compress_level'] = PNG_COMPRESSION
            elif des_path.suffix.lower() == '.gif':
                target_format = 'GIF'
                if getattr(img, 'is_animated', False):
                     save_options['save_all'] = True
                     img.seek(0)
                     save_options['duration'] = img.info.get('duration', 100)
                     save_options['loop'] = img.info.get('loop', 0)
            else:
                 target_format = Image.registered_extensions().get(des_path.suffix.lower()) or original_format
                 logger.debug(f"Using fallback target format '{target_format}' for {des_path.name}")

            if target_format:
                des_path.parent.mkdir(parents=True, exist_ok=True)
                img.save(des_path, format=target_format, **save_options)
            else:
                logger.warning(f"No target format determined for saving {filepath.name}. Skipping save.")
                # If we didn't save, the destination wasn't created by us.
                return filepath, False

        if des_path.exists():
            try:
                optimized_size = des_path.stat().st_size
            except OSError as e:
                 logger.error(f"Could not get stats for destination file {des_path}: {e}")
                 # Treat as failure if we can't verify the result
                 return filepath, False

            # Determine if the destination file should be used
            use_destination = (
                optimized_size < original_size or # Smaller size
                resized or                      # Image dimensions changed
                will_change_format              # Format is different
            )

            if use_destination:
                 logger.debug(f"Optimization successful for {filepath.name}. Using {des_path.name} ({original_size} -> {optimized_size} bytes)")
                 return des_path, True
            else:
                 logger.debug(f"Optimization did not improve {filepath.name} sufficiently ({original_size} -> {optimized_size} bytes). Using original.")
                 if filepath != des_path: # Remove the generated file if it's not the source
                     try:
                         os.unlink(des_path)
                     except OSError as e:
                         logger.warning(f"Could not remove ineffective optimized file {des_path}: {e}")
                 return filepath, False
        else:
             logger.error(f"Destination file {des_path.name} not found after save attempt.")
             return filepath, False

    except UnidentifiedImageError:
        logger.warning(f"Pillow could not identify image file: {filepath.name}. Skipping optimization.")
        return filepath, False
    except Exception as e:
        logger.error(f"Error optimizing {filepath.name}: {str(e)}", exc_info=False)
        # Clean up potentially partially created destination file
        if des_path and des_path.exists() and des_path != filepath:
            try:
                os.unlink(des_path)
                logger.debug(f"Cleaned up failed optimization file: {des_path.name}")
            except OSError as unlink_err:
                 logger.error(f"Error cleaning up failed optimization file {des_path.name}: {unlink_err}")
        return filepath, False
 
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
        new_filepath = filepath_obj.parent / sanitized_name
        try:
            # Check for collision before rename
            if new_filepath.exists() and not filepath_obj.samefile(new_filepath):
                 logger.warning(f"Sanitized filename {sanitized_name} already exists. Skipping rename/processing for {filename}.")
                 return None, None, False
            elif not new_filepath.exists():
                 shutil.move(str(filepath_obj), str(new_filepath))
                 filepath_obj = new_filepath
                 filename = sanitized_name
                 logger.info(f"Renamed invalid file '{filename}' to '{sanitized_name}'")

        except Exception as e:
            logger.error(f"Failed to rename invalid file '{filename}' to '{sanitized_name}': {str(e)}")
            return None, None, False
    
    current_filepath_obj = filepath_obj
    current_filename = filename

    # fix mislabeled .webp files (caused by a previous bug lol)
    if OPTIMIZATION_AVAILABLE and current_filename.lower().endswith('.webp'):
        actual_format = None
        try:
            with Image.open(current_filepath_obj) as img:
                img.load()
                actual_format = img.format # e.g., 'JPEG', 'PNG', 'WEBP', None
        except UnidentifiedImageError:
             logger.warning(f"File '{current_filename}' has .webp extension, but Pillow cannot identify it as a valid image. It might be corrupt or not an image.")
             actual_format = None
        except Exception as img_err:
            logger.warning(f"Could not open file '{current_filename}' with Pillow to verify format: {img_err}. Skipping correction check.")
            actual_format = None

        if actual_format and actual_format != 'WEBP':
            logger.warning(f"File '{current_filename}' has .webp extension but Pillow identified format as {actual_format}. Attempting auto-correction.")

            correct_extension = FORMAT_TO_EXTENSION.get(actual_format)

            if correct_extension:
                correct_filename_path = current_filepath_obj.with_suffix(correct_extension)
                logger.info(f"Attempting rename: '{current_filename}' -> '{correct_filename_path.name}'.")
                try:
                    # Handle collision before renaming
                    target_path_str = str(correct_filename_path)
                    final_target_path = correct_filename_path # Store the final intended path obj

                    if os.path.exists(target_path_str) and not current_filepath_obj.samefile(correct_filename_path):
                         base = correct_filename_path.stem
                         counter = 1
                         # Find a unique name like file_fix1.jpg, file_fix2.jpg etc.
                         while True:
                             temp_name = f"{base}_fix{counter}{correct_extension}"
                             temp_path = current_filepath_obj.with_name(temp_name)
                             if not temp_path.exists():
                                 final_target_path = temp_path
                                 break
                             counter += 1
                             if counter > 10: # Safety break
                                 raise OSError("Could not find a unique filename after 10 attempts.")
                         logger.warning(f"Target '{correct_filename_path.name}' existed. Renaming to '{final_target_path.name}'.")

                    # Perform the rename using the final determined path
                    shutil.move(str(current_filepath_obj), str(final_target_path))

                    # Update variables to reflect the corrected file
                    current_filepath_obj = final_target_path
                    current_filename = final_target_path.name
                    logger.info(f"Successfully auto-corrected mislabeled WebP to '{current_filename}'.")

                except Exception as rename_err:
                    logger.error(f"Failed to auto-correct mislabeled file '{filename}' to '{correct_filename_path.name}': {rename_err}. Proceeding with original problematic file.")
                    # Keep original current_filepath_obj and current_filename
            else:
                logger.warning(f"Unknown format '{actual_format}' identified by Pillow for '{current_filename}'. Cannot map to extension for auto-correction.")
                # Proceed with the file as-is (still mislabeled .webp)

        elif actual_format == 'WEBP':
            logger.debug(f"File '{current_filename}' confirmed as WEBP by Pillow.")

    current_filepath_str = str(current_filepath_obj)
            
    if not can_optimize():
        logger.debug("Optimization skipped: Pillow not available.")
        return str(filepath_obj), filename, False
    
    file_extension = current_filepath_obj.suffix.lower()
    is_optimizable_input = file_extension in OPTIMIZABLE_INPUT_EXTENSIONS # Reuse list from optimize_image
    
    if is_optimizable_input:        
        intended_webp_filename = f"{current_filepath_obj.stem}.webp"
        intended_webp_filepath = current_filepath_obj.with_name(intended_webp_filename)

        final_path_obj, used_webp_destination = optimize_image(
            current_filepath_obj,
            destination=intended_webp_filepath,
            convert_to_webp=True
        )

        final_filepath_str = str(final_path_obj)
        final_filename = final_path_obj.name

        optimization_occurred = used_webp_destination

        return final_filepath_str, final_filename, optimization_occurred
    
    else:
        # Not an image type we attempt to optimize
        logger.debug(f"Skipping optimization for non-image or unsupported type: {current_filename}")
        return current_filepath_str, current_filename, False

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