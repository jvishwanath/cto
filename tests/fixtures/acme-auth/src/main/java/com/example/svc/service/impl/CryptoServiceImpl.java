package com.example.svc.service.impl;

import java.io.IOException;
import java.security.cert.Certificate;

import org.springframework.stereotype.Service;

import com.example.svc.service.CryptoService;
import com.example.svc.util.CryptoUtils;

@Service
public class CryptoServiceImpl implements CryptoService {

    @Override
    public void validateCSR(String csrStrEscaped) throws IOException {
        if (csrStrEscaped == null || csrStrEscaped.isBlank()) {
            throw new IOException("CSR is empty");
        }
        CryptoUtils.parsePem(csrStrEscaped);
    }

    @Override
    public Certificate signCertificate(String csrStrEscaped)
            throws IOException {
        validateCSR(csrStrEscaped);
        return CryptoUtils.sign(csrStrEscaped);
    }

    @Override
    public boolean verifyJwt(String token) {
        return token != null && token.split("\\.").length == 3;
    }
}
