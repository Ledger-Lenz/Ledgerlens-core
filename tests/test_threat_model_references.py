import os
import re
import pathlib

def test_threat_model_file_references():
    """Verify that all file paths referenced in docs/threat_model.md exist."""
    # Locate the threat model file
    root_dir = pathlib.Path(__file__).parent.parent.resolve()
    threat_model_path = root_dir / "docs" / "threat_model.md"
    
    assert threat_model_path.exists(), f"docs/threat_model.md does not exist at {threat_model_path}"
    
    with open(threat_model_path, "r", encoding="utf-8") as f:
        content = f.read()
        
    # Extract markdown links with file:/// scheme
    # e.g., [text](file:///c:/Users/HP/Ledgerlens-core/path/to/file#L10)
    file_links = re.findall(r"file:///([^\)\s#]+)", content)
    
    # Extract backtick references that look like files (contain a slash or file extension)
    backtick_refs = re.findall(r"`([^`\s]+)`", content)
    file_backticks = []
    for ref in backtick_refs:
        if "/" in ref or any(ref.endswith(ext) for ext in [".py", ".md", ".rs", ".txt", ".json", ".yaml", ".yml"]):
            file_backticks.append(ref)
            
    all_refs = set()
    
    # Process file:/// links
    for link in file_links:
        link_path = link.replace("\\", "/")
        if "Ledgerlens-core/" in link_path:
            rel_path = link_path.split("Ledgerlens-core/")[-1]
        elif "ledgerlens-core/" in link_path:
            rel_path = link_path.split("ledgerlens-core/")[-1]
        elif ":" in link_path:
            p = pathlib.Path(link_path)
            try:
                rel_path = p.relative_to(root_dir).as_posix()
            except ValueError:
                rel_path = link_path
        else:
            rel_path = link_path
        all_refs.add(rel_path)
        
    # Process backtick refs
    for ref in file_backticks:
        ref_path = ref.replace("\\", "/")
        if "/" in ref_path or any(ref_path.endswith(ext) for ext in [".py", ".md", ".rs", ".txt", ".json", ".yaml", ".yml"]):
            all_refs.add(ref_path)
            
    # Filter out anything that is clearly not a file reference or is external
    cleaned_refs = []
    for ref in all_refs:
        if ref.startswith("http") or ref.startswith("www") or ref.startswith("/"):
            continue
        ref = ref.strip(".,;")
        cleaned_refs.append(ref)
        
    missing_files = []
    for ref in cleaned_refs:
        target_path = root_dir / ref
        if not target_path.exists():
            abs_path = pathlib.Path(ref)
            if not abs_path.exists():
                missing_files.append((ref, str(target_path)))
                
    assert not missing_files, (
        f"The following file references in docs/threat_model.md are broken:\n"
        + "\n".join([f"- '{ref}' (resolved to '{resolved}')" for ref, resolved in missing_files])
    )
