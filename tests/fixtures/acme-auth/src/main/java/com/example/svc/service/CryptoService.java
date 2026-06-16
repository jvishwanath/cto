package com.example.svc.service;

import java.io.IOException;
import java.security.cert.Certificate;

/**
 * Certificate signing service interface. Implemented by
 * {@link com.example.svc.service.impl.CryptoServiceImpl}.
 */
public interface CryptoService {

    void validateCSR(String csrStrEscaped) throws IOException;

    Certificate signCertificate(String csrStrEscaped) throws IOException;

    boolean verifyJwt(String token);
}
