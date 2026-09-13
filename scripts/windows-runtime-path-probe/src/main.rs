use std::{fs, io, path::Path};

fn main() {
    assert!(cfg!(windows), "Windows-only native acceptance probe");
    let inside = fs::canonicalize("inside.txt").expect("canonicalize inside");
    assert!(inside.is_absolute());
    let text = fs::read_to_string(&inside).expect("reopen canonical inside");
    assert!(text.starts_with("OMS_LPAC_INSIDE_") && text.ends_with("\r\n"));
    assert_eq!(
        fs::read_to_string("../outside.txt")
            .unwrap_err()
            .raw_os_error(),
        Some(5)
    );
    match fs::canonicalize("../outside.txt") {
        Ok(outside) => assert_eq!(
            fs::read_to_string(outside).unwrap_err().raw_os_error(),
            Some(5)
        ),
        Err(error) => assert_eq!(error.raw_os_error(), Some(5)),
    }
    assert_eq!(
        fs::write(&inside, b"forbidden").unwrap_err().raw_os_error(),
        Some(5)
    );
    assert_eq!(fs::read_to_string(&inside).expect("reread inside"), text);
    assert_eq!(
        fs::canonicalize("missing.txt").unwrap_err().kind(),
        io::ErrorKind::NotFound
    );
    let home = fs::canonicalize("home").expect("canonicalize home");
    let marker = home.join("canon-é-患者.txt");
    fs::write(&marker, b"OMS_PATH_PROOF").expect("write canonical home");
    let marker = fs::canonicalize(marker).expect("canonicalize Unicode filename");
    assert_eq!(
        fs::read(marker).expect("reopen canonical Unicode filename"),
        b"OMS_PATH_PROOF"
    );
    assert!(inside.starts_with(fs::canonicalize(Path::new(".")).expect("canonicalize parent")));
    println!("OMS_WINDOWS_CANONICALIZE_PASSED");
}
