# **AnkiCollab Media Server: Global Specification & Roadmap**  

### **Overview**  
The **AnkiCollab Media Server** extends the collaborative AnkiCollab platform by enabling users to **store, retrieve, and manage media files** (images (& audio?)) efficiently. Given the constraints of free hosting, it prioritizes **storage optimization, bandwidth reduction, and security** while ensuring a decent experience for users contributing and consuming shared Anki decks.  

---

## **1. Global Specification**  

### **Core Features**  
**Client Upload & Download**  
- Users can upload media when suggesting new cards.  
- Users can download missing media for decks they subscribed to.  
- Supports **hash-based deduplication** to avoid duplicate file storage.  

**Backend Storage & Optimization**  
- Uses **S3-compatible object storage** for efficient and scalable storage.  
- Files are **hashed on the client-side** to determine uniqueness before upload.  
- Automatic **conversion to WebP** format to reduce file size.  
- **Reference tracking** ensures media is deleted if no approved card references it.  

**Efficient Bulk Media Retrieval**  
- New deck subscribers may need **thousands of media files**.  
- Uses **batch processing & compression** (e.g., ZIP or streaming downloads) to optimize bandwidth.  

**Security & Access Control**  
- Uses **pre-signed S3 URLs** to control access to media files.  
- Uploads are only allowed for **approved** files to prevent abuse.  

**Web Interface for Media Management**  
- Maintainers can **view, search, and manage** media linked to their decks via the AnkiCollab website.  

---

## **2. Architecture & Flow**  

### **Client ↔ Backend ↔ S3 Interaction**  

#### **Upload Flow** (New Media File)  
1. **Client calculates the hash** of the file (SHA-256, not md5).  
2. **Client sends hash to backend** → Backend checks if it already exists.  
3. **If missing:**  
   - Backend issues an **upload token (pre-signed S3 URL)**.  
   - Client uploads the file to S3 directly:
        -  Only image files (.png, .jpg, .jpeg, .gif, .bmp, .webp, etc.) are allowed. Limit Max file size to X MB?
        -  Backend (or S3 Lambda) converts images to WebP (if not already WebP) to further reduce size?
        - Upload shuold be done in a background process so users can continue reviewing material or doing other things in anki.
4. **Backend tracks the media reference** in PostgreSQL (associated with a pending suggestion or approved note (auto-approved for example)).  
   - There should be a table that tracks both the hash (unique file identifier) and the ankicollab note ids that use it. Files shouldn't be owned by single notes, since many files are reused across decks (local forks of popular decks for example) and we have to be careful about what we store and minimize redundancy. The overhead shouldn't be too huge for a lookup and it can significantly reduce total storage use
   - Maybe we can also store that hash in the metadata in S3 directly. Not sure

#### **Download Flow** (Retrieving Media)  
1. Client requests media for a deck → Backend finds matching files by translating the association: Deck + File name into the hash / unique file ids in S3.  
2. Backend issues **pre-signed URLs** for direct S3 downloads.  
3. For bulk downloads, backend **bundles files into a compressed archive**? Instead of zipping, I'd use parallel streaming with batched requests. 

#### **Housekeeping Flow** (Removing Unused Media)  
- If a suggestion is **declined**, **updated** or a **card is deleted**, reference counts are updated.  
- If a file has **zero references**, it is deleted from both the database & S3.  
- A **cron job** periodically removes orphaned media.  

### **Database Schema Proposal**
```
CREATE TABLE stored_media_files (
    hash TEXT PRIMARY KEY,         -- SHA-256 hash
    s3_key TEXT UNIQUE,             -- S3 object path
    reference_count INT DEFAULT 1   -- Number of times used
);
```

The current anki.media table should be altered to also reference stored_media_files unique_id/hash

That way the backend can resolve media (Looks up the hash corresponding to example.jpg within Deck A):
``SELECT s3_key FROM stored_media_files WHERE hash IN (
    SELECT media_hash FROM anki.media WHERE deck = 1234 AND filename = 'example.jpg'
);``

We can handle new media files: 
```
INSERT INTO media_files (hash, s3_key, reference_count)
VALUES ('abc123...', 'abc123...', 1);
INSERT INTO anki.media (filename, deck, hash)
VALUES ('example.jpg', 1234, 'abc123...');
```

---

## **3. Roadmap & Contribution Guide**  

### **Phase 1: Core Implementation (MVP)**
- [ ] **Client Integration** (Hashing & Upload Requests)  
- [ ] **Backend API for Media Management**  
  - Upload verification, reference tracking  
  - Pre-signed URL generation for uploads  
- [ ] **S3 Storage Setup** (with WebP conversion)  
- [ ] **Basic Housekeeping** (Auto-delete unused media)  

### **Phase 2: Optimization & Bulk Handling**
- [ ] **Batch Download System** (Zip or parallel download strategy)  
- [ ] **Automatic Media Conversion** (JPEG → WebP on upload to reduce file sizes)  
- [ ] **Compression Strategies** for downloads  
- [ ] **Access Control & Abuse Prevention** (Rate limits, upload restrictions)  

### **Phase 3: Web UI & Advanced Features**
- [ ] **Maintainers should be able to remove files on the website (no new uploads) and it should handle the reference counter accordingly
- [ ] **Files should be previewable on the website

---

## **4. How to Contribute**  
**Stack:** Rust (backend), PostgreSQL, S3, WebP processing, Anki API (Python)  

**Repos:**  
- Backend → [AnkiCollab-Backend](https://github.com/CravingCrates/AnkiCollab-Backend)  
- Client → [AnkiCollab-Plugin](https://github.com/CravingCrates/AnkiCollab-Plugin)  
- Web → [AnkiCollab-Website](https://github.com/CravingCrates/AnkiCollab-Website)  

**Contribution Areas:**  
- Backend API development  
- PostgreSQL schema optimizations  
- S3 storage handling & compression strategies  
- Web UI for maintainers  

**Next Steps:**  
1. Clone the repos & set up a local dev environment.  
2. Check open issues & roadmap milestones.  
3. Discuss this process before implementation.  
4. Implement
5. ??
6. Profit
