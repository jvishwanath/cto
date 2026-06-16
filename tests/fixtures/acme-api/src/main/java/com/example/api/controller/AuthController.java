package com.example.api.controller;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
public class AuthController {

    @PostMapping("/v1/auth/token")
    public String issueToken(String credentials) {
        return "jwt." + credentials.hashCode();
    }

    @GetMapping("/v1/auth/verify")
    public boolean verify(String token) {
        return token != null && token.startsWith("jwt.");
    }

    @GetMapping("/health")
    public String health() {
        return "ok";
    }
}
