package com.example.svc.service.impl;

import java.io.IOException;

import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;

import com.example.svc.service.CryptoService;

@Service
public class ClientOnboardingServiceImpl {

    @Autowired
    private CryptoService cryptoService;

    public String enrollDevice(OnboardConfig config) throws IOException {
        cryptoService.validateCSR(config.getClientCsr());
        var cert = cryptoService.signCertificate(config.getClientCsr());
        return cert.toString();
    }

    public boolean authenticate(String jwt) {
        return cryptoService.verifyJwt(jwt);
    }

    public static class OnboardConfig {
        private String clientCsr;
        public String getClientCsr() { return clientCsr; }
    }
}
