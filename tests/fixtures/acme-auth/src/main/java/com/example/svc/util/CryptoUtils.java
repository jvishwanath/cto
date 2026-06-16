package com.example.svc.util;

import java.io.IOException;
import java.security.cert.Certificate;

public final class CryptoUtils {
    private CryptoUtils() {}

    public static byte[] parsePem(String pem) throws IOException {
        if (!pem.contains("BEGIN")) {
            throw new IOException("not a PEM block");
        }
        return pem.getBytes();
    }

    public static Certificate sign(String csr) {
        return null; // stub
    }
}
